"""Exhaustive tests for the pure decision functions — the risk rails are
the product's safety case, so every rail gets both a pass and a fail test."""

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from qt.services.engine import (
    RISK_DEFAULTS,
    Candidate,
    RailContext,
    check_rails,
    evaluate_entry,
    evaluate_exit,
)

ET = ZoneInfo("America/New_York")
NOON_ET = datetime(2026, 7, 13, 12, 0, tzinfo=ET)


def cand(**kw) -> Candidate:
    base = dict(symbol="TEST", asset_class="stock", price=20.0, change_pct=5.0, vwap=19.5)
    base.update(kw)
    return Candidate(**base)


def params(**overrides) -> dict:
    p = {
        "entry": {
            "min_day_gain_pct": 3.0,
            "require_above_vwap": True,
            "entry_window_start": "10:00",
            "entry_window_end": "15:30",
        },
        "exit": {
            "trailing_stop_pct": 5.0,
            "stop_loss_pct": 4.0,
            "take_profit_pct": 10.0,
            "max_holding_hours": 0,
            "flatten_before_close": False,
            "exit_below_vwap": False,
        },
    }
    for section, values in overrides.items():
        p[section].update(values)
    return p


def ctx(**kw) -> RailContext:
    base = dict(
        equity=5000.0,
        open_positions_total=0,
        open_exposure_usd=0.0,
        open_positions_strategy=0,
        open_exposure_strategy_usd=0.0,
        entries_today=0,
        already_open_symbol=False,
        last_loss_at=None,
        loss_sale_within_31d=False,
        risk=dict(RISK_DEFAULTS),
        leverage_unlocked=False,
        daily_loss_usd=0.0,
    )
    base.update(kw)
    return RailContext(**base)


STRAT = {"max_positions": 3, "sleeve_usd": 1000.0}


# ---- entry rules ----

def test_entry_passes():
    ok, reason = evaluate_entry(params(), cand(), NOON_ET)
    assert ok and "up 5.00%" in reason


def test_entry_rejects_weak_gain():
    ok, reason = evaluate_entry(params(), cand(change_pct=2.0), NOON_ET)
    assert not ok and "< required" in reason


def test_entry_rejects_below_vwap():
    ok, reason = evaluate_entry(params(), cand(price=19.0), NOON_ET)
    assert not ok and "VWAP" in reason


def test_entry_rejects_missing_vwap_when_required():
    ok, reason = evaluate_entry(params(), cand(vwap=None), NOON_ET)
    assert not ok and "VWAP unavailable" in reason


def test_entry_rejects_outside_window():
    early = datetime(2026, 7, 13, 9, 35, tzinfo=ET)
    ok, reason = evaluate_entry(params(), cand(), early)
    assert not ok and "entry window" in reason


def test_entry_no_window_means_any_time():
    p = params(entry={"entry_window_start": None, "entry_window_end": None})
    ok, _ = evaluate_entry(p, cand(), datetime(2026, 7, 13, 3, 0, tzinfo=ET))
    assert ok


# ---- risk rails: one pass test, then a fail test per rail ----

def test_rails_all_pass():
    ok, reason = check_rails(STRAT, 200.0, ctx())
    assert ok and "all rails passed" in reason


def test_rail_duplicate_position():
    ok, reason = check_rails(STRAT, 200.0, ctx(already_open_symbol=True))
    assert not ok and "already open" in reason


def test_rail_trade_rate():
    ok, reason = check_rails(STRAT, 200.0, ctx(entries_today=RISK_DEFAULTS["max_trades_per_day"]))
    assert not ok and "trade-rate" in reason


def test_rail_max_total_positions():
    ok, reason = check_rails(STRAT, 200.0, ctx(open_positions_total=RISK_DEFAULTS["max_total_positions"]))
    assert not ok and "max open positions" in reason


def test_rail_strategy_max_positions():
    ok, reason = check_rails(STRAT, 200.0, ctx(open_positions_strategy=3))
    assert not ok and "strategy max positions" in reason


def test_rail_sleeve_budget():
    ok, reason = check_rails(STRAT, 200.0, ctx(open_exposure_strategy_usd=900.0))
    assert not ok and "sleeve budget" in reason


def test_rail_no_leverage_caps_at_equity():
    # equity 1000, exposure 900 + 200 would need margin — blocked even though
    # max_total_exposure_usd (3000) would allow it
    ok, reason = check_rails(STRAT, 200.0, ctx(equity=1000.0, open_exposure_usd=900.0))
    assert not ok and "no-leverage rail" in reason


def test_rail_leverage_enabled_and_unlocked_raises_cap():
    risk = dict(RISK_DEFAULTS, leverage_enabled=True)
    ok, _ = check_rails(
        STRAT, 200.0, ctx(equity=1000.0, open_exposure_usd=900.0, risk=risk, leverage_unlocked=True)
    )
    assert ok  # 1100 ≤ max_total_exposure_usd 3000


def test_rail_leverage_enabled_but_env_locked_still_capped():
    risk = dict(RISK_DEFAULTS, leverage_enabled=True)
    ok, reason = check_rails(
        STRAT, 200.0, ctx(equity=1000.0, open_exposure_usd=900.0, risk=risk, leverage_unlocked=False)
    )
    assert not ok and "no-leverage rail" in reason


def test_rail_daily_loss_kill_switch():
    ok, reason = check_rails(STRAT, 200.0, ctx(daily_loss_usd=200.0))
    assert not ok and "kill switch" in reason


def test_rail_daily_loss_pct_binds_before_usd():
    # 5% of $1000 equity = $50 — hit even though the $200 USD limit isn't
    ok, reason = check_rails(STRAT, 200.0, ctx(equity=1000.0, daily_loss_usd=60.0))
    assert not ok and "kill switch" in reason


def test_rail_cooldown_after_loss():
    recent = datetime.now(timezone.utc) - timedelta(hours=2)
    ok, reason = check_rails(STRAT, 200.0, ctx(last_loss_at=recent))
    assert not ok and "cooldown" in reason


def test_rail_cooldown_expired_is_fine():
    old = datetime.now(timezone.utc) - timedelta(hours=30)
    ok, _ = check_rails(STRAT, 200.0, ctx(last_loss_at=old))
    assert ok


def test_rail_wash_sale_block():
    ok, reason = check_rails(STRAT, 200.0, ctx(loss_sale_within_31d=True))
    assert not ok and "wash-sale" in reason


def test_rail_wash_sale_warn_mode_allows_with_warning():
    risk = dict(RISK_DEFAULTS, wash_sale_guard="warn")
    ok, reason = check_rails(STRAT, 200.0, ctx(loss_sale_within_31d=True, risk=risk))
    assert ok and "wash-sale warning" in reason


def test_rail_wash_sale_off():
    risk = dict(RISK_DEFAULTS, wash_sale_guard="off")
    ok, reason = check_rails(STRAT, 200.0, ctx(loss_sale_within_31d=True, risk=risk))
    assert ok and "warning" not in reason


# ---- exit rules ----

NOW = datetime(2026, 7, 14, 15, 0, tzinfo=timezone.utc)
YESTERDAY = NOW - timedelta(days=1)


def test_exit_stop_loss_always_fires():
    should, reason = evaluate_exit(
        params(), True, 100.0, NOW - timedelta(hours=1), 100.0, 95.9, None, NOW, False
    )
    assert should and "stop-loss" in reason


def test_exit_trailing_stop():
    # entered 100, ran to 120, now 113 → 5.8% off the high
    should, reason = evaluate_exit(params(), True, 100.0, YESTERDAY, 120.0, 113.0, None, NOW, False)
    assert should and "trailing stop" in reason


def test_exit_take_profit():
    should, reason = evaluate_exit(params(), True, 100.0, YESTERDAY, 111.0, 110.5, None, NOW, False)
    assert should and "take-profit" in reason


def test_swing_mode_suppresses_soft_exits_same_day():
    same_day_entry = NOW - timedelta(hours=2)
    should, _ = evaluate_exit(params(), True, 100.0, same_day_entry, 111.0, 110.5, None, NOW, False)
    assert not should  # take-profit waits until tomorrow in swing mode


def test_intraday_mode_takes_profit_same_day():
    same_day_entry = NOW - timedelta(hours=2)
    should, reason = evaluate_exit(params(), False, 100.0, same_day_entry, 111.0, 110.5, None, NOW, False)
    assert should and "take-profit" in reason


def test_exit_below_vwap():
    p = params(exit={"exit_below_vwap": True})
    should, reason = evaluate_exit(p, True, 100.0, YESTERDAY, 104.0, 102.0, 103.0, NOW, False)
    assert should and "VWAP" in reason


def test_exit_max_holding_period():
    p = params(exit={"max_holding_hours": 48})
    should, reason = evaluate_exit(
        p, True, 100.0, NOW - timedelta(hours=50), 101.0, 100.5, None, NOW, False
    )
    assert should and "max holding" in reason


def test_exit_flatten_before_close():
    p = params(exit={"flatten_before_close": True})
    should, reason = evaluate_exit(p, False, 100.0, NOW - timedelta(hours=2), 101.0, 100.5, None, NOW, True)
    assert should and "flatten" in reason


def test_no_exit_when_healthy():
    should, _ = evaluate_exit(params(), True, 100.0, YESTERDAY, 105.0, 104.5, None, NOW, False)
    assert not should
