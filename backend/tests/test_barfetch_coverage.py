"""Which bar fetches go through the local cache.

The cache is what makes a second backtest cheap: read what's stored, download
only the missing edge. A path that quietly bypasses it re-downloads a year of
history on every run and nobody notices — it just feels slow. These tests pin
the coverage so a bypass has to be deliberate.
"""

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
from qt.services import backtest as bt_service
from qt.services import barcache


@pytest.fixture()
def fresh_cache(monkeypatch):
    """A private, empty bar cache for one test."""
    eng = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    barcache.CacheBase.metadata.create_all(eng)
    Sess = sessionmaker(bind=eng, expire_on_commit=False)
    monkeypatch.setattr(barcache, "_engine", eng)
    monkeypatch.setattr(barcache, "_Session", Sess)
    return Sess


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


def _daily(n: int = 20) -> list[dict]:
    """n CLOSED daily bars ending yesterday (today's is still open — never cached)."""
    end = datetime.now(timezone.utc) - timedelta(days=1)
    out = []
    for i in range(n):
        d = end - timedelta(days=n - 1 - i)
        c = 100 + i
        out.append({"t": d.strftime("%Y-%m-%dT05:00:00Z"), "o": c, "h": c, "l": c,
                    "c": c, "v": 1e6, "vw": c})
    return out


def _strategy() -> dict:
    return {
        "name": "cache cover", "asset_class": "stock", "universe": "custom",
        "symbols": ["NVDA"], "preset": "custom",
        "params": {
            "entry": {"min_day_gain_pct": 3, "require_above_vwap": False,
                      "entry_window_start": None, "entry_window_end": None},
            "exit": {"trailing_stop_pct": 5, "stop_loss_pct": 4, "take_profit_pct": 0,
                     "max_holding_hours": 0, "flatten_before_close": False, "exit_below_vwap": False},
        },
        "sizing_usd": 1000, "sleeve_usd": 5000, "max_positions": 3,
        "swing_mode": False, "ignore_regime": True,
    }


def test_benchmark_is_identical_served_from_cache(fresh_cache):
    """SPY now comes through the cache. A cached daily bar is RE-STAMPED, so the
    real risk isn't a miss — it's the benchmark landing a day off the equity
    curve. Same bars in, same series out, whichever side they came from."""
    # The window must START where the bars do: the cache distrusts a series whose
    # head is more than a few days inside the requested window (it can't tell a
    # short history from a hole), and re-fetches. 20 bars ending yesterday means
    # the oldest is 20 days back, so ask for 21.
    start = (datetime.now(timezone.utc) - timedelta(days=21)).strftime("%Y-%m-%dT%H:%M:%SZ")
    bars = _daily()
    days = [b["t"][:10] for b in bars]

    import asyncio

    # First call: nothing cached, everything downloaded (and saved).
    fetch = AsyncMock(return_value={"SPY": bars})
    with patch.object(AlpacaClient, "historical_bars", new=fetch):
        cold = asyncio.run(bt_service.fetch_benchmark(AlpacaClient("k", "s"), "stock", start, days))
    assert fetch.await_count == 1

    # Second call: served from the cache, with no download at all.
    fetch2 = AsyncMock(return_value={"SPY": []})
    with patch.object(AlpacaClient, "historical_bars", new=fetch2):
        warm = asyncio.run(bt_service.fetch_benchmark(AlpacaClient("k", "s"), "stock", start, days))

    assert warm == cold, "cached benchmark disagrees with the freshly fetched one"
    assert fetch2.await_count == 0, "re-downloaded SPY despite a warm cache"


def test_a_second_backtest_downloads_nothing(client, configured, fresh_cache):
    """The whole point of the cache: run it once, then run it again for free."""
    sid = client.post("/api/strategies", json=_strategy()).json()["id"]
    req = {"strategy_id": sid, "symbols": ["NVDA"], "days": 20,
           "timeframe": "1Day", "starting_cash": 5000, "spread_pct": 0}
    bars = _daily()

    cold = AsyncMock(return_value={"NVDA": bars, "SPY": bars})
    with patch.object(AlpacaClient, "historical_bars", new=cold):
        assert client.post("/api/backtest", json=req).status_code == 200
    assert cold.await_count > 0  # first run pays for the download

    warm = AsyncMock(return_value={})
    with patch.object(AlpacaClient, "historical_bars", new=warm):
        assert client.post("/api/backtest", json=req).status_code == 200
    assert warm.await_count == 0, "second identical backtest re-downloaded its bars"


def test_the_portfolio_backtest_uses_the_cache_too(client, configured, fresh_cache):
    """It fetches every symbol of every picked strategy — the heaviest download
    in the app, and the last one that was still bypassing the cache."""
    sid = client.post("/api/strategies", json=_strategy()).json()["id"]
    req = {"strategy_ids": [sid], "days": 20, "timeframe": "1Day",
           "starting_cash": 5000, "spread_pct": 0}
    bars = _daily()

    cold = AsyncMock(return_value={"NVDA": bars, "SPY": bars})
    with patch.object(AlpacaClient, "historical_bars", new=cold):
        assert client.post("/api/backtest/portfolio", json=req).status_code == 200

    warm = AsyncMock(return_value={})
    with patch.object(AlpacaClient, "historical_bars", new=warm):
        assert client.post("/api/backtest/portfolio", json=req).status_code == 200
    assert warm.await_count == 0, "second portfolio backtest re-downloaded its bars"
