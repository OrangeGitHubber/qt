"""Watchdog staleness decision (pure) + graceful-shutdown gating in the tick."""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest

from qt import security
from qt.broker.alpaca import SECRET_KEY_ID, SECRET_KEY_SECRET, AlpacaClient
from qt.db import session_scope
from qt.models import Trade
from qt.services import lifecycle, regime, scanner, watchdog
from qt.services.engine import tick
from qt.services.watchdog import should_alert, tick_is_stale
from qt.settings_service import get_setting, set_setting

NOW = datetime(2026, 7, 18, 15, 0, tzinfo=timezone.utc)
THRESH = timedelta(minutes=5)


# ---- pure staleness ----


def test_stale_when_never_ticked():
    assert tick_is_stale(None, NOW, THRESH) is True


def test_not_stale_when_recent():
    assert tick_is_stale(NOW - timedelta(minutes=2), NOW, THRESH) is False


def test_stale_when_old():
    assert tick_is_stale(NOW - timedelta(minutes=10), NOW, THRESH) is True


def test_naive_timestamp_treated_as_utc():
    assert tick_is_stale(NOW.replace(tzinfo=None) - timedelta(minutes=10), NOW, THRESH) is True


@pytest.mark.parametrize(
    "mode,market_open,last,already,expected",
    [
        ("off", True, None, False, False),  # engine off -> never
        ("paper", False, None, False, False),  # market closed -> never
        ("paper", True, None, True, False),  # already alerted -> don't spam
        ("paper", True, NOW - timedelta(minutes=10), False, True),  # stale + open -> alert
        ("paper", True, NOW - timedelta(minutes=1), False, False),  # fresh -> no alert
        ("shadow", True, None, False, True),  # shadow still ticks; stale -> alert
    ],
)
def test_should_alert_table(mode, market_open, last, already, expected):
    assert (
        should_alert(
            mode=mode,
            market_open=market_open,
            last_tick_at=last,
            now=NOW,
            threshold=THRESH,
            already_alerted=already,
        )
        is expected
    )


# ---- shutdown gating in the real tick ----

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
SPY_BULL = {"SPY": [{"c": 500.0 - i * 0.1} for i in range(210)]}

STRATEGY = {
    "name": "Shutdown test",
    "asset_class": "stock",
    "universe": "scanner",
    "preset": "custom",
    "params": {
        "entry": {"min_day_gain_pct": 3, "require_above_vwap": True,
                  "entry_window_start": None, "entry_window_end": None},
        "exit": {"trailing_stop_pct": 5, "stop_loss_pct": 4, "take_profit_pct": 12,
                 "max_holding_hours": 0, "flatten_before_close": False, "exit_below_vwap": False},
    },
    "sizing_usd": 200, "sleeve_usd": 1000, "max_positions": 3,
    "swing_mode": True, "ignore_regime": False,
}


@pytest.fixture()
def shutdown_world(client):
    scanner.invalidate_cache()
    regime.invalidate_cache()
    lifecycle.reset()
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
    lifecycle.reset()
    scanner.invalidate_cache()
    regime.invalidate_cache()


async def test_shutdown_flag_blocks_new_entries_but_records_heartbeat(client, shutdown_world):
    common = dict(
        account=AsyncMock(return_value=ACCOUNT),
        clock=AsyncMock(return_value=CLOCK_OPEN),
        stock_movers=AsyncMock(return_value=MOVERS),
        crypto_assets=AsyncMock(return_value=[]),
        crypto_snapshots=AsyncMock(return_value={}),
        stock_bars=AsyncMock(return_value=SPY_BULL),
        stock_snapshots=AsyncMock(return_value=SNAPSHOT_UP),
    )
    # Shutdown requested BEFORE the tick: no entry should be opened.
    lifecycle.request_shutdown()
    with patch.multiple(AlpacaClient, **common):
        await tick(leverage_unlocked=False)

    journal = client.get("/api/engine/journal?mode=shadow").json()
    assert journal == []  # entries skipped during shutdown

    # But the heartbeat was still recorded (the tick ran to completion).
    with session_scope() as s:
        assert get_setting(s, "last_tick_at") is not None

    # With the flag cleared, the same tick opens the position normally.
    lifecycle.reset()
    with patch.multiple(AlpacaClient, **common):
        await tick(leverage_unlocked=False)
    journal = client.get("/api/engine/journal?mode=shadow").json()
    assert len([t for t in journal if t["status"] == "open"]) == 1
