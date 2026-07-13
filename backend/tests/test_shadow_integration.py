"""End-to-end shadow mode: a full engine tick against a mocked Alpaca —
entry decision, journal row with reasons, then a price collapse and exit."""

from unittest.mock import AsyncMock, patch

import pytest

from qt import security
from qt.broker.alpaca import SECRET_KEY_ID, SECRET_KEY_SECRET, AlpacaClient
from qt.db import session_scope
from qt.models import Trade
from qt.services import regime, scanner
from qt.services.engine import tick
from qt.settings_service import set_setting

ACCOUNT = {"equity": "5000", "cash": "5000"}
CLOCK_OPEN = {"is_open": True, "next_close": "2099-01-01T21:00:00Z"}

MOVERS = {"gainers": [{"symbol": "ROCKET", "percent_change": 6.0, "change": 1.2, "price": 21.2}], "losers": []}
SNAPSHOT_UP = {
    "ROCKET": {
        "latestTrade": {"p": 21.2},
        "dailyBar": {"c": 21.2, "v": 5_000_000, "vw": 20.8},
        "prevDailyBar": {"c": 20.0},
    }
}
SNAPSHOT_CRASH = {
    "ROCKET": {
        "latestTrade": {"p": 20.0},  # -5.7% from entry → stop-loss (4%)
        "dailyBar": {"c": 20.0, "v": 6_000_000, "vw": 20.9},
        "prevDailyBar": {"c": 20.0},
    }
}
SPY_BARS_BULL = {"SPY": [{"c": 500.0 - i * 0.1} for i in range(210)]}  # last close far above MA

STRATEGY = {
    "name": "Shadow test",
    "asset_class": "stock",
    "universe": "scanner",
    "preset": "custom",
    "params": {
        "entry": {"min_day_gain_pct": 3, "require_above_vwap": True,
                  "entry_window_start": None, "entry_window_end": None},
        "exit": {"trailing_stop_pct": 5, "stop_loss_pct": 4, "take_profit_pct": 12,
                 "max_holding_hours": 0, "flatten_before_close": False, "exit_below_vwap": False},
    },
    "sizing_usd": 200,
    "sleeve_usd": 1000,
    "max_positions": 3,
    "swing_mode": True,
    "ignore_regime": False,
}


@pytest.fixture()
def shadow_world(client):
    scanner.invalidate_cache()
    regime.invalidate_cache()
    with session_scope() as s:
        security.set_secret(s, SECRET_KEY_ID, "k")
        security.set_secret(s, SECRET_KEY_SECRET, "s")
        set_setting(s, "engine_mode", "shadow")
        set_setting(s, "risk_config", {})
    resp = client.post("/api/strategies", json=STRATEGY)
    sid = resp.json()["id"]
    client.post(f"/api/strategies/{sid}/toggle")
    yield sid
    with session_scope() as s:
        set_setting(s, "engine_mode", "off")
        s.query(Trade).delete()
        security.delete_secret(s, SECRET_KEY_ID)
        security.delete_secret(s, SECRET_KEY_SECRET)
    client.delete(f"/api/strategies/{sid}")
    scanner.invalidate_cache()
    regime.invalidate_cache()


async def test_shadow_entry_then_stop_loss_exit(client, shadow_world):
    common = dict(
        account=AsyncMock(return_value=ACCOUNT),
        clock=AsyncMock(return_value=CLOCK_OPEN),
        stock_movers=AsyncMock(return_value=MOVERS),
        crypto_assets=AsyncMock(return_value=[]),
        crypto_snapshots=AsyncMock(return_value={}),
        stock_bars=AsyncMock(return_value=SPY_BARS_BULL),
    )

    # Tick 1: price is strong → shadow entry
    with patch.multiple(AlpacaClient, stock_snapshots=AsyncMock(return_value=SNAPSHOT_UP), **common):
        await tick(leverage_unlocked=False)

    journal = client.get("/api/engine/journal?mode=shadow").json()
    opens = [t for t in journal if t["status"] == "open"]
    assert len(opens) == 1
    trade = opens[0]
    assert trade["symbol"] == "ROCKET"
    assert trade["qty"] == 9  # int(200 // 21.2)
    assert "up 6.00%" in trade["entry_reason"]
    assert "all rails passed" in trade["entry_reason"]

    # Tick 2: same price → duplicate-position rail blocks a second entry, no exit
    with patch.multiple(AlpacaClient, stock_snapshots=AsyncMock(return_value=SNAPSHOT_UP), **common):
        await tick(leverage_unlocked=False)
    journal = client.get("/api/engine/journal?mode=shadow").json()
    assert len([t for t in journal if t["status"] == "open"]) == 1

    # Tick 3: price collapses below the stop → shadow exit with reason + P&L
    with patch.multiple(AlpacaClient, stock_snapshots=AsyncMock(return_value=SNAPSHOT_CRASH), **common):
        await tick(leverage_unlocked=False)

    journal = client.get("/api/engine/journal?mode=shadow").json()
    closed = [t for t in journal if t["status"] == "closed"]
    assert len(closed) == 1
    assert "stop-loss" in closed[0]["exit_reason"]
    assert closed[0]["pnl"] == pytest.approx((20.0 - 21.2) * 9, abs=0.01)


async def test_shadow_regime_blocks_entries_in_bear_market(client, shadow_world):
    regime.invalidate_cache()
    spy_bear = {"SPY": [{"c": 400.0}] + [{"c": 500.0}] * 209}  # last close far below MA
    with patch.multiple(
        AlpacaClient,
        account=AsyncMock(return_value=ACCOUNT),
        clock=AsyncMock(return_value=CLOCK_OPEN),
        stock_movers=AsyncMock(return_value=MOVERS),
        stock_snapshots=AsyncMock(return_value=SNAPSHOT_UP),
        crypto_assets=AsyncMock(return_value=[]),
        crypto_snapshots=AsyncMock(return_value={}),
        stock_bars=AsyncMock(return_value=spy_bear),
    ):
        await tick(leverage_unlocked=False)

    journal = client.get("/api/engine/journal?mode=shadow").json()
    assert journal == []  # no entries, not even rejected rows — strategy was regime-gated
