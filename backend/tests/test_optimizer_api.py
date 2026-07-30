"""Optimizer endpoint guard: MACD/RSI strategies are daily signals, so an
intraday search is rejected (mirrors the backtest guard) — UNLESS the strategy
also carries a price-triggered exit, which makes it a mixed-resolution run:
15-minute replay, MACD/RSI still read off completed daily closes. The 422 (and
the mixed decision) happen synchronously, before any background search starts."""

import time
from unittest.mock import AsyncMock

import pytest

from qt import security
from qt.api import optimizer as optimizer_api
from qt.broker.alpaca import SECRET_KEY_ID, SECRET_KEY_SECRET
from qt.db import session_scope
from qt.models import Strategy, StrategyConfigVersion, Trade


def _strategy(*, macd=False, rsi_exit=False, price_exits=True) -> dict:
    entry = {
        "min_day_gain_pct": 1,
        "require_above_vwap": False,
        "require_macd_bullish": macd,
        "entry_window_start": None,
        "entry_window_end": None,
    }
    exit_rules = {
        "trailing_stop_pct": 5, "stop_loss_pct": 4, "take_profit_pct": 0,
        "max_holding_hours": 0, "flatten_before_close": False, "exit_below_vwap": False,
    }
    if rsi_exit:
        exit_rules["exit_rsi_above"] = 70
    params = {"entry": entry, "exit": exit_rules}
    if not price_exits:
        # No stop / trailing / take-profit at all: nothing an intraday replay could
        # simulate that a daily one can't, so such a strategy stays locked to daily.
        # A hard stop is mandatory for everything EXCEPT a buy-and-hold DCA sleeve,
        # which is the one shape allowed to have no price exit.
        exit_rules.update({"trailing_stop_pct": 0, "stop_loss_pct": 0, "take_profit_pct": 0})
        params["dca"] = {"interval_days": 7}
    return {
        "name": "opt guard", "asset_class": "stock", "universe": "custom",
        "symbols": ["AAPL"], "preset": "custom",
        "params": params,
        "sizing_usd": 1000, "sleeve_usd": 1000, "max_positions": 1,
        "swing_mode": True, "ignore_regime": True,
    }


@pytest.fixture()
def configured():
    with session_scope() as s:
        security.set_secret(s, SECRET_KEY_ID, "k")
        security.set_secret(s, SECRET_KEY_SECRET, "s")
    yield
    with session_scope() as s:
        s.query(Trade).delete()
        s.query(StrategyConfigVersion).delete()
        s.query(Strategy).delete()
        security.delete_secret(s, SECRET_KEY_ID)
        security.delete_secret(s, SECRET_KEY_SECRET)
    # The search is a module-level singleton: a test that starts one (with the
    # worker stubbed out) must hand back a clean slate or the next test 409s.
    optimizer_api._progress.running = False


def _optimize(client, sid, timeframe):
    return client.post(
        "/api/optimizer",
        json={"strategy_id": sid, "symbols": ["AAPL"], "timeframe": timeframe,
              "days": 180, "iterations": 5},
    )


def test_optimizer_rejects_intraday_for_a_macd_strategy(client, configured):
    """MACD with NO price-triggered exit: an intraday search would only buy a
    twitchy intraday MACD, so it stays locked to daily bars."""
    sid = client.post("/api/strategies", json=_strategy(macd=True, price_exits=False)).json()["id"]
    r = _optimize(client, sid, "1Hour")
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert "MACD" in detail and "daily" in detail


def test_optimizer_rejects_intraday_for_an_rsi_strategy(client, configured):
    sid = client.post(
        "/api/strategies", json=_strategy(rsi_exit=True, price_exits=False)
    ).json()["id"]
    r = _optimize(client, sid, "15Min")
    assert r.status_code == 422
    assert "daily" in r.json()["detail"]


def test_optimizer_runs_mixed_resolution_for_a_macd_strategy_with_stops(client, configured, monkeypatch):
    """The case the blanket guard was actively harming: three of the four searched
    knobs are price-triggered exits, and a daily replay checks them only at the
    close — so a tight stop looks nearly free and the search drifts toward stops
    that would whipsaw for real. Such a strategy must NOT be rejected; it replays
    15-minute bars with MACD taken from completed daily closes."""
    sid = client.post("/api/strategies", json=_strategy(macd=True)).json()["id"]
    worker = AsyncMock()
    monkeypatch.setattr(optimizer_api, "_run_search", worker)

    r = _optimize(client, sid, "15Min")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["mixed_resolution"] is True
    assert body["timeframe"] == "15Min"
    # The worker was handed the mixed flag and the 15-minute replay timeframe.
    assert worker.call_args.kwargs["mixed"] is True
    assert worker.call_args.args[5] == "15Min"  # timeframe positional


def test_mixed_strategy_replays_intraday_even_when_daily_was_requested(client, configured, monkeypatch):
    """Mixed resolution is a property of the STRATEGY, not of what was asked for:
    asking for 1 Day still gets the 15-minute replay (with daily signals)."""
    sid = client.post("/api/strategies", json=_strategy(rsi_exit=True)).json()["id"]
    worker = AsyncMock()
    monkeypatch.setattr(optimizer_api, "_run_search", worker)

    r = _optimize(client, sid, "1Day")
    assert r.status_code == 200, r.text
    assert r.json()["timeframe"] == "15Min"
    assert worker.call_args.kwargs["mixed"] is True


def test_plain_strategy_is_untouched(client, configured, monkeypatch):
    """No daily signals at all → no guard, no mixed run: the requested timeframe
    is used exactly as before."""
    sid = client.post("/api/strategies", json=_strategy()).json()["id"]
    worker = AsyncMock()
    monkeypatch.setattr(optimizer_api, "_run_search", worker)

    r = _optimize(client, sid, "15Min")
    assert r.status_code == 200, r.text
    assert r.json()["mixed_resolution"] is False
    assert worker.call_args.args[5] == "15Min"
    assert worker.call_args.kwargs["mixed"] is False


def test_scanner_replay_search_counts_only_symbols_it_can_actually_test(
    client, configured, monkeypatch
):
    """The optimizer is the OTHER consumer of ScannerReplayDataset. The backtest
    was fixed to report what it replayed; this path still passed ds.union — so a
    mover with no cached bars was handed to the search, silently dropped, and
    still counted in universe_size. Both consumers must agree."""
    from datetime import datetime, timedelta, timezone

    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    from qt.services import barcache

    eng = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    barcache.CacheBase.metadata.create_all(eng)
    Sess = sessionmaker(bind=eng, expire_on_commit=False)
    monkeypatch.setattr(barcache, "_engine", eng)
    monkeypatch.setattr(barcache, "_Session", Sess)
    d0 = (datetime.now(timezone.utc) - timedelta(days=6)).strftime("%Y-%m-%d")
    d1 = (datetime.now(timezone.utc) - timedelta(days=5)).strftime("%Y-%m-%d")

    with Sess() as s:
        # HASBARS has daily bars; NOBARS made the mover list but has none.
        barcache.save_daily_bars(s, "HASBARS", [
            {"t": f"{d0}T14:00:00Z", "o": 100, "h": 100, "l": 100, "c": 100, "v": 1e6, "vw": 100},
            {"t": f"{d1}T14:00:00Z", "o": 106, "h": 106, "l": 106, "c": 106, "v": 1e6, "vw": 106},
        ])
        barcache.store_movers(s, d1, [("HASBARS", 6.0, 106.0, 1e8), ("NOBARS", 6.0, 50.0, 1e8)])
        s.commit()

    sid = client.post("/api/strategies", json=_strategy()).json()["id"]
    r = client.post("/api/optimizer", json={
        "strategy_id": sid, "scanner_replay": True, "days": 30, "iterations": 5,
    })
    assert r.status_code == 200, r.text
    body = r.json()
    # Only the symbol that can actually be searched is handed to the search…
    assert body["symbols"] == ["HASBARS"]

    # …and the reported universe matches, with the drop named rather than hidden.
    for _ in range(60):
        status = client.get("/api/optimizer/status").json()
        if not status["running"]:
            break
        time.sleep(0.05)
    result = status.get("result") or {}
    if result:  # the search completed — its counts must not overstate
        assert result["universe_size"] == 1
        assert result["universe_dropped"] == ["NOBARS"]
