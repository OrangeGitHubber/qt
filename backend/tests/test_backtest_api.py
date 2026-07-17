"""Backtest endpoint: benchmark selection, incl. not drawing the tested
symbol twice."""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest

from qt import security
from qt.broker.alpaca import SECRET_KEY_ID, SECRET_KEY_SECRET, AlpacaClient
from qt.db import session_scope
from qt.models import Strategy, StrategyConfigVersion, Trade


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
