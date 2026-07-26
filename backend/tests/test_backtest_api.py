"""Backtest endpoint: benchmark selection, incl. not drawing the tested
symbol twice."""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from qt import security
from qt.broker.alpaca import SECRET_KEY_ID, SECRET_KEY_SECRET, AlpacaClient
from qt.db import session_scope
from qt.models import Strategy, StrategyConfigVersion, Trade
from qt.services import barcache


def hourly(closes: list[float], symbol_days: int = 6) -> list[dict]:
    start = datetime.now(timezone.utc) - timedelta(days=symbol_days)
    return [
        {
            "t": (start + timedelta(hours=i * 6)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "o": c, "h": c * 1.01, "l": c * 0.99, "c": c, "v": 1000, "vw": c,
        }
        for i, c in enumerate(closes)
    ]


BARS = hourly([100, 100, 100, 104, 106, 108, 108, 108])


def _strategy(asset_class: str) -> dict:
    return {
        "name": f"bt {asset_class}",
        "asset_class": asset_class,
        "universe": "scanner",
        "preset": "custom",
        "params": {
            "entry": {"min_day_gain_pct": 3, "require_above_vwap": False,
                      "entry_window_start": None, "entry_window_end": None},
            "exit": {"trailing_stop_pct": 5, "stop_loss_pct": 4, "take_profit_pct": 0,
                     "max_holding_hours": 0, "flatten_before_close": False, "exit_below_vwap": False},
        },
        "sizing_usd": 1000, "sleeve_usd": 5000, "max_positions": 3,
        "swing_mode": False, "ignore_regime": True,
    }


@pytest.fixture()
def configured(client):
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


def _make(client, asset_class: str) -> int:
    return client.post("/api/strategies", json=_strategy(asset_class)).json()["id"]


def test_market_benchmark_skipped_when_it_is_the_tested_symbol(client, configured):
    """A BTC/USD strategy tested on BTC/USD must not plot BTC/USD twice."""
    sid = _make(client, "crypto")
    fetch = AsyncMock(return_value={"BTC/USD": BARS})
    with patch.object(AlpacaClient, "historical_bars", new=fetch):
        body = client.post(
            "/api/backtest",
            json={"strategy_id": sid, "symbols": ["BTC/USD"], "days": 30,
                  "timeframe": "1Hour", "starting_cash": 5000, "spread_pct": 0},
        ).json()
    assert body["benchmark"] is None
    assert body["benchmark_symbol"] is None
    assert body["hold_benchmark_label"] == "BTC/USD"   # the one true comparison
    assert fetch.await_count == 1                      # no wasted benchmark fetch


def test_market_benchmark_kept_when_it_differs(client, configured):
    """A stock strategy on NVDA still gets SPY as the market line."""
    sid = _make(client, "stock")
    fetch = AsyncMock(side_effect=[{"NVDA": BARS}, {"SPY": BARS}])
    with patch.object(AlpacaClient, "historical_bars", new=fetch):
        body = client.post(
            "/api/backtest",
            json={"strategy_id": sid, "symbols": ["NVDA"], "days": 30,
                  "timeframe": "1Hour", "starting_cash": 5000, "spread_pct": 0},
        ).json()
    assert body["benchmark_symbol"] == "SPY"
    assert body["hold_benchmark_label"] == "NVDA"
    assert fetch.await_count == 2


@pytest.fixture()
def seeded_cache(monkeypatch):
    """Point the global bar cache at a fresh in-memory DB and seed two recent
    trading days of daily bars + reconstructed movers."""
    eng = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    barcache.CacheBase.metadata.create_all(eng)
    Sess = sessionmaker(bind=eng, expire_on_commit=False)
    monkeypatch.setattr(barcache, "_engine", eng)
    monkeypatch.setattr(barcache, "_Session", Sess)

    # Two consecutive recent days: D0 baseline, D1 the +gain day.
    d0 = (datetime.now(timezone.utc) - timedelta(days=6)).strftime("%Y-%m-%d")
    d1 = (datetime.now(timezone.utc) - timedelta(days=5)).strftime("%Y-%m-%d")
    with Sess() as s:
        for sym in ("MOVER", "OTHER"):
            barcache.save_daily_bars(s, sym, [
                {"t": f"{d0}T14:00:00Z", "o": 100, "h": 100, "l": 100, "c": 100, "v": 1e6, "vw": 100},
                {"t": f"{d1}T14:00:00Z", "o": 105, "h": 105, "l": 105, "c": 105, "v": 1e6, "vw": 105},
            ])
        # Only MOVER made the top-N on D1 → OTHER must be gated out.
        barcache.store_movers(s, d1, [("MOVER", 5.0, 105.0, 1e8)])
        s.commit()
    return d1


def test_scanner_replay_gates_to_cached_movers(client, configured, seeded_cache):
    """End-to-end: replay uses the cached day's movers, not a symbol list. Only
    MOVER was a riser on D1, so OTHER (same +5%) must never trade."""
    sid = _make(client, "stock")
    fetch = AsyncMock(side_effect=Exception("no benchmark in test"))  # SPY best-effort
    with patch.object(AlpacaClient, "historical_bars", new=fetch):
        body = client.post(
            "/api/backtest",
            json={"strategy_id": sid, "scanner_replay": True, "days": 30,
                  "starting_cash": 5000, "spread_pct": 0},
        ).json()
    assert body["scanner_replay"] is True
    assert body["universe_size"] == 1        # one unique mover across the window
    assert body["days_replayed"] == 1
    assert body["timeframe"] == "1Day"
    assert body["trades"] == 1               # MOVER entered; OTHER gated out
    assert body["trade_list"][0]["symbol"] == "MOVER"


def test_scanner_replay_top_n_narrows_the_eligible_movers(client, configured, monkeypatch):
    """replay_top_n narrows each day's eligible risers at read time — no re-sweep.
    Three symbols all qualify on D1; top_n=1 lets only the rank-1 name trade."""
    eng = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    barcache.CacheBase.metadata.create_all(eng)
    Sess = sessionmaker(bind=eng, expire_on_commit=False)
    monkeypatch.setattr(barcache, "_engine", eng)
    monkeypatch.setattr(barcache, "_Session", Sess)
    d0 = (datetime.now(timezone.utc) - timedelta(days=6)).strftime("%Y-%m-%d")
    d1 = (datetime.now(timezone.utc) - timedelta(days=5)).strftime("%Y-%m-%d")
    with Sess() as s:
        for sym, c1 in (("TOP", 106), ("MID", 105), ("LOW", 104)):
            barcache.save_daily_bars(s, sym, [
                {"t": f"{d0}T14:00:00Z", "o": 100, "h": 100, "l": 100, "c": 100, "v": 1e6, "vw": 100},
                {"t": f"{d1}T14:00:00Z", "o": c1, "h": c1, "l": c1, "c": c1, "v": 1e6, "vw": c1},
            ])
        barcache.store_movers(s, d1, [  # stored wide; ranked TOP > MID > LOW
            ("TOP", 6.0, 106.0, 1e8), ("MID", 5.0, 105.0, 1e8), ("LOW", 4.0, 104.0, 1e8),
        ])
        s.commit()

    def run(top_n):
        fetch = AsyncMock(side_effect=Exception("no benchmark"))
        with patch.object(AlpacaClient, "historical_bars", new=fetch):
            return client.post("/api/backtest", json={
                "strategy_id": sid, "scanner_replay": True, "replay_top_n": top_n,
                "days": 30, "starting_cash": 50000, "spread_pct": 0}).json()

    sid = _make(client, "stock")
    one = run(1)
    assert one["replay_top_n"] == 1 and one["universe_size"] == 1
    assert [t["symbol"] for t in one["trade_list"]] == ["TOP"]
    three = run(3)
    assert three["universe_size"] == 3
    assert {t["symbol"] for t in three["trade_list"]} == {"TOP", "MID", "LOW"}


def test_scanner_replay_needs_a_sweep_first(client, configured, monkeypatch):
    """With an empty cache, replay explains itself instead of silently doing nothing."""
    eng = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    barcache.CacheBase.metadata.create_all(eng)
    monkeypatch.setattr(barcache, "_engine", eng)
    monkeypatch.setattr(barcache, "_Session", sessionmaker(bind=eng, expire_on_commit=False))
    sid = _make(client, "stock")
    r = client.post("/api/backtest", json={"strategy_id": sid, "scanner_replay": True, "days": 30})
    assert r.status_code == 422
    assert "sweep" in r.json()["detail"].lower()


def test_scanner_replay_rejects_crypto(client, configured):
    sid = _make(client, "crypto")
    r = client.post("/api/backtest", json={"strategy_id": sid, "scanner_replay": True, "days": 30})
    assert r.status_code == 422
    assert "stocks-only" in r.json()["detail"].lower()


def test_market_benchmark_kept_for_a_basket_including_it(client, configured):
    """BTC + ETH basket: 'hold the basket' and 'hold BTC' are different facts."""
    sid = _make(client, "crypto")
    fetch = AsyncMock(side_effect=[{"BTC/USD": BARS, "ETH/USD": BARS}, {"BTC/USD": BARS}])
    with patch.object(AlpacaClient, "historical_bars", new=fetch):
        body = client.post(
            "/api/backtest",
            json={"strategy_id": sid, "symbols": ["BTC/USD", "ETH/USD"], "days": 30,
                  "timeframe": "1Hour", "starting_cash": 5000, "spread_pct": 0},
        ).json()
    assert body["benchmark_symbol"] == "BTC/USD"
    assert body["hold_benchmark_label"] == "2 symbols (equal weight)"
