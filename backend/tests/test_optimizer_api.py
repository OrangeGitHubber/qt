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


# THE "no price exits" SHAPE IS NOW REFUSED EARLIER, and by a stronger guard.
#
# `price_exits=False` has to set `dca.interval_days` — StrategyParams makes a
# hard stop mandatory for everything EXCEPT a DCA sleeve, so a strategy with no
# price exit IS a DCA sleeve, necessarily. Since 2026-08-07 the optimizer
# refuses those outright (400), because the live engine never calls
# `evaluate_entry` for one and a search would be tuning rules that do nothing.
#
# So these two now assert the 400. The daily-lock branch they used to cover is
# unreachable in its no-price-exits form for the same reason — it can only be
# entered by a strategy that is turned away one step earlier. Its OTHER branches
# stay reachable and are covered by the mixed-resolution tests below.
def test_a_macd_dca_sleeve_is_refused_before_the_resolution_guard(client, configured):
    sid = client.post("/api/strategies", json=_strategy(macd=True, price_exits=False)).json()["id"]
    r = _optimize(client, sid, "1Hour")
    assert r.status_code == 400, r.text
    assert "DCA sleeve" in r.json()["detail"]


def test_an_rsi_dca_sleeve_is_refused_too(client, configured):
    sid = client.post(
        "/api/strategies", json=_strategy(rsi_exit=True, price_exits=False)
    ).json()["id"]
    r = _optimize(client, sid, "15Min")
    assert r.status_code == 400, r.text
    assert "DCA sleeve" in r.json()["detail"]


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

    # Scanner replay is now decided by the STRATEGY'S universe, not a request
    # flag — the search tests the universe the strategy actually trades. Same
    # behaviour under test; only the way it's switched on has moved.
    scanner_strategy = {**_strategy(), "universe": "scanner", "symbols": [], "top_n": 10}
    sid = client.post("/api/strategies", json=scanner_strategy).json()["id"]
    r = client.post("/api/optimizer", json={"strategy_id": sid, "days": 30, "iterations": 5})
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


def test_a_posted_symbol_list_cannot_replace_the_strategys_universe(client, configured, monkeypatch):
    """The universe is the strategy's, and the SERVER decides it.

    Hiding the picker in the UI isn't the guarantee — the endpoint is reachable
    directly. Tuning a strategy against a substituted symbol list fits settings
    to names it doesn't trade, which is the precise overfitting the
    out-of-sample split exists to catch; it must not be reachable at all.
    """
    from unittest.mock import AsyncMock, patch

    from qt.broker.alpaca import AlpacaClient

    sid = client.post("/api/strategies", json=_strategy()).json()["id"]  # custom universe: AAPL
    with patch.object(AlpacaClient, "historical_bars", new=AsyncMock(return_value={})):
        r = client.post("/api/optimizer", json={
            "strategy_id": sid,
            "symbols": ["TSLA", "NVDA", "MSFT"],   # ignored
            "days": 30, "iterations": 5,
        })
    assert r.status_code == 200, r.text
    assert r.json()["symbols"] == ["AAPL"], "a posted symbol list overrode the strategy's universe"


def test_scanner_replay_cannot_be_forced_on_a_non_scanner_strategy(client, configured):
    """Replay is a property of a scanner strategy, not a checkbox. Forcing it on
    a basket/custom strategy would search a universe that strategy never trades."""
    from unittest.mock import AsyncMock, patch

    from qt.broker.alpaca import AlpacaClient

    sid = client.post("/api/strategies", json=_strategy()).json()["id"]  # custom universe
    with patch.object(AlpacaClient, "historical_bars", new=AsyncMock(return_value={})):
        r = client.post("/api/optimizer", json={
            "strategy_id": sid, "scanner_replay": True, "days": 30, "iterations": 5,
        })
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["scanner_replay"] is False
    assert body["symbols"] == ["AAPL"]


def test_the_search_tops_up_intraday_bars_and_reports_what_it_replayed(configured, monkeypatch):
    """The optimizer tops up the cache in its BACKGROUND TASK, not in the request
    — the download is far too slow to hold a request open for (Cloudflare cuts
    the origin off at 100s). So the task ends up holding a DIFFERENT dataset than
    the request handler saw, and everything derived from it — bars, timeframe,
    mixed — has to be recomputed. Miss that and the search silently replays daily
    bars while reporting 15-minute ones, which is worse than not topping up.

    Driven through _run_search directly: under TestClient the event loop is torn
    down when the response returns, which cancels the background task, so an
    end-to-end poll can only ever assert conditionally (see the test above)."""
    import asyncio
    from datetime import datetime, timedelta, timezone

    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    from qt.api.backtest import load_scanner_replay_dataset, replay_inputs
    from qt.services import barcache

    eng = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    barcache.CacheBase.metadata.create_all(eng)
    Sess = sessionmaker(bind=eng, expire_on_commit=False)
    monkeypatch.setattr(barcache, "_engine", eng)
    monkeypatch.setattr(barcache, "_Session", Sess)
    d0 = (datetime.now(timezone.utc) - timedelta(days=6)).strftime("%Y-%m-%d")
    d1 = (datetime.now(timezone.utc) - timedelta(days=5)).strftime("%Y-%m-%d")

    with Sess() as s:
        barcache.save_daily_bars(s, "HASBARS", [
            {"t": f"{d0}T14:00:00Z", "o": 100, "h": 100, "l": 100, "c": 100, "v": 1e6, "vw": 100},
            {"t": f"{d1}T14:00:00Z", "o": 106, "h": 106, "l": 106, "c": 106, "v": 1e6, "vw": 106},
        ])
        barcache.store_movers(s, d1, [("HASBARS", 6.0, 106.0, 1e8)])
        s.commit()

    def ib(ts, c):
        return {"t": ts, "o": c, "h": c, "l": c, "c": c, "v": 1e5, "vw": c}

    intraday = {"HASBARS": [ib(f"{d0}T14:00:00Z", 100), ib(f"{d0}T18:00:00Z", 101),
                            ib(f"{d1}T14:00:00Z", 104), ib(f"{d1}T18:00:00Z", 106)]}

    class FakeClient:
        async def historical_bars(self, *a, **kw):
            return intraday

    strategy_dict = {
        "asset_class": "stock", "swing_mode": True, "sizing_usd": 1000,
        "sleeve_usd": 1000, "max_positions": 1, "params": _strategy()["params"],
    }
    ds = load_scanner_replay_dataset("stock", 30, 10, None)
    assert ds.used_intraday is False  # the state the request handler would see
    inputs = replay_inputs(ds, strategy_dict["params"], 10)

    # _progress is a module singleton and earlier tests in this file leave their
    # background task's error/result on it. Clear it, or this test reads someone
    # else's outcome.
    optimizer_api._progress.error = None
    optimizer_api._progress.result = None

    # A REAL risk config, not {}. This used to pass an empty dict and got away
    # with it only because the out-of-sample slice never reached a rail check:
    # with no sim_start the slice carried no warm-up, so its first day had no
    # day-gain baseline and every bar was skipped. Now that the slice can trade,
    # `{}` raises KeyError('max_trades_per_day') inside check_rails — the empty
    # dict was never a valid input, it was only never exercised.
    from qt.services.engine import RISK_DEFAULTS

    asyncio.run(optimizer_api._run_search(
        FakeClient(), strategy_dict, dict(RISK_DEFAULTS), ds.replayed, "stock",
        inputs["timeframe"], 30, 5, 5000, 0.1,
        prebuilt_bars=inputs["bars"], prebuilt_daily=inputs["daily"],
        eligible_by_day=inputs["eligible_by_day"], replay_extra=inputs["extra"],
        replay_ctx={"ds": ds, "asset_class": "stock", "days": 30,
                    "replay_top_n": 10, "scanner_cfg": None},
        mixed=inputs["mixed"],
    ))

    assert optimizer_api._progress.error is None
    result = optimizer_api._progress.result
    assert result["intraday_topped_up"] is True
    assert result["replay_intraday"] is True
    assert result["timeframe"] == "15Min"   # what it ACTUALLY replayed
