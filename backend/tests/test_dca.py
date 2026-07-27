"""DCA baseline sleeve: the always-on dollar-cost-averaging strategy.

These exercise the scheduling + lot logic through a full engine tick against a
mocked Alpaca (same style as test_shadow_integration):

- buys the fixed list when never bought,
- skips while still inside the interval,
- opens a fresh INDEPENDENT lot once the interval has elapsed — even though a
  prior lot for the same symbol is still open (the one rail DCA bypasses),
- still honours every OTHER rail (here: the sleeve budget).
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

from qt import security
from qt.broker.alpaca import SECRET_KEY_ID, SECRET_KEY_SECRET, AlpacaClient
from qt.db import session_scope
from qt.models import Trade
from qt.services import regime, scanner
from qt.services.engine import tick
from qt.settings_service import set_setting

ACCOUNT = {"equity": "5000", "cash": "5000"}
CLOCK_OPEN = {"is_open": True, "next_close": "2099-01-01T21:00:00Z"}

# Prices low enough that $200 buys at least one whole share.
SNAPSHOT = {
    "SPY": {"latestTrade": {"p": 50.0}, "dailyBar": {"c": 50.0, "vw": 49.5}, "prevDailyBar": {"c": 49.0}},
    "QQQ": {"latestTrade": {"p": 40.0}, "dailyBar": {"c": 40.0, "vw": 39.5}, "prevDailyBar": {"c": 39.0}},
}
SPY_BARS_BULL = {"SPY": [{"c": 500.0 - i * 0.1} for i in range(210)]}

DCA_STRATEGY = {
    "name": "DCA test",
    "asset_class": "stock",
    "universe": "custom",
    "symbols": ["SPY", "QQQ"],
    "preset": "dca_sleeve",
    "params": {
        "entry": {"min_day_gain_pct": 0, "require_above_vwap": False,
                  "entry_window_start": None, "entry_window_end": None},
        # All exits off — buy-and-hold lots (allowed only because dca is set).
        "exit": {"trailing_stop_pct": 0, "stop_loss_pct": 0, "take_profit_pct": 0,
                 "max_holding_hours": 0, "flatten_before_close": False, "exit_below_vwap": False},
        "dca": {"interval_days": 7},
    },
    "sizing_usd": 200,
    "sleeve_usd": 1000,  # room for ~5 lots
    "max_positions": 5,
    "swing_mode": True,
    "ignore_regime": False,
}

COMMON = dict(
    account=AsyncMock(return_value=ACCOUNT),
    clock=AsyncMock(return_value=CLOCK_OPEN),
    stock_movers=AsyncMock(return_value={"gainers": [], "losers": []}),
    crypto_assets=AsyncMock(return_value=[]),
    crypto_snapshots=AsyncMock(return_value={}),
    stock_bars=AsyncMock(return_value=SPY_BARS_BULL),
    historical_bars=AsyncMock(return_value={}),
    stock_snapshots=AsyncMock(return_value=SNAPSHOT),
)


def _make_dca_strategy(client, **overrides):
    scanner.invalidate_cache()
    regime.invalidate_cache()
    with session_scope() as s:
        security.set_secret(s, SECRET_KEY_ID, "k")
        security.set_secret(s, SECRET_KEY_SECRET, "s")
        set_setting(s, "engine_mode", "shadow")
        set_setting(s, "risk_config", {})
    body = {**DCA_STRATEGY, **overrides}
    sid = client.post("/api/strategies", json=body).json()["id"]
    client.post(f"/api/strategies/{sid}/toggle")
    return sid


def _teardown(client, sid):
    with session_scope() as s:
        set_setting(s, "engine_mode", "off")
        set_setting(s, "risk_config", {})
        s.query(Trade).delete()
        security.delete_secret(s, SECRET_KEY_ID)
        security.delete_secret(s, SECRET_KEY_SECRET)
    client.delete(f"/api/strategies/{sid}")
    scanner.invalidate_cache()
    regime.invalidate_cache()


def _open_lots(client):
    journal = client.get("/api/engine/journal?mode=shadow").json()
    return [t for t in journal if t["status"] == "open"]


def _backdate_all(days: int) -> None:
    """Push every open lot's entry_at into the past so the interval elapses."""
    with session_scope() as s:
        for t in s.query(Trade).filter(Trade.status == "open").all():
            t.entry_at = datetime.now(timezone.utc) - timedelta(days=days)


async def test_dca_buys_fixed_list_then_skips_within_interval(client):
    sid = _make_dca_strategy(client)
    try:
        # Tick 1: never bought → one lot per symbol.
        with patch.multiple(AlpacaClient, **COMMON):
            await tick(leverage_unlocked=False)
        lots = _open_lots(client)
        assert sorted(t["symbol"] for t in lots) == ["QQQ", "SPY"]
        assert all("DCA scheduled lot (every 7d)" in t["entry_reason"] for t in lots)

        # Tick 2 immediately: still inside the 7-day interval → no new lots.
        with patch.multiple(AlpacaClient, **COMMON):
            await tick(leverage_unlocked=False)
        assert len(_open_lots(client)) == 2
    finally:
        _teardown(client, sid)


async def test_dca_opens_independent_lot_after_interval(client):
    sid = _make_dca_strategy(client)
    try:
        with patch.multiple(AlpacaClient, **COMMON):
            await tick(leverage_unlocked=False)
        assert len(_open_lots(client)) == 2

        # Interval elapses while the first lots are still open → a NEW lot opens
        # for each symbol (proves the already-open-symbol rail is bypassed and
        # that lots are independent single-entry Trades, not an averaged basis).
        _backdate_all(days=8)
        with patch.multiple(AlpacaClient, **COMMON):
            await tick(leverage_unlocked=False)

        lots = _open_lots(client)
        assert len(lots) == 4
        assert sum(1 for t in lots if t["symbol"] == "SPY") == 2
        assert sum(1 for t in lots if t["symbol"] == "QQQ") == 2
    finally:
        _teardown(client, sid)


async def test_dca_still_respects_sleeve_budget_rail(client):
    # Sleeve only fits a single $200 lot: the first symbol buys, the second is
    # rail-rejected. DCA bypasses ONLY the already-open rail, never the sleeve.
    sid = _make_dca_strategy(client, sleeve_usd=300)
    try:
        with patch.multiple(AlpacaClient, **COMMON):
            await tick(leverage_unlocked=False)

        journal = client.get("/api/engine/journal?mode=shadow").json()
        opens = [t for t in journal if t["status"] == "open"]
        rejected = [t for t in journal if t["status"] == "rejected"]
        assert len(opens) == 1
        assert len(rejected) == 1
        assert "sleeve budget exceeded" in rejected[0]["entry_reason"]
    finally:
        _teardown(client, sid)
