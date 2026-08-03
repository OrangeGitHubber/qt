"""Exhaustive tests for the pure decision functions — the risk rails are
the product's safety case, so every rail gets both a pass and a fail test."""

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from qt.services.engine import (
    NONFILL_COOLDOWN_MAX_HOURS,
    RISK_DEFAULTS,
    Candidate,
    RailContext,
    check_rails,
    evaluate_entry,
    evaluate_exit,
    nonfill_cooldown_hours,
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


def test_entry_rejects_over_extended_gain():
    # up 22% with a 10% ceiling → skipped as too extended to chase (CONL case)
    ok, reason = evaluate_entry(params(entry={"max_day_gain_pct": 10}), cand(change_pct=22.0), NOON_ET)
    assert not ok and "too extended" in reason


def test_entry_max_gain_off_by_default():
    # 0 / absent means no ceiling — even a huge riser passes the gain checks
    ok, _ = evaluate_entry(params(), cand(change_pct=40.0), NOON_ET)
    assert ok


def test_entry_rejects_above_max_price():
    # "only movers under $10" — a $20 stock is skipped
    ok, reason = evaluate_entry(params(entry={"max_price": 10}), cand(price=20.0), NOON_ET)
    assert not ok and "> max $10" in reason


def test_entry_rejects_below_min_price():
    ok, reason = evaluate_entry(params(entry={"min_price": 5}), cand(price=3.0, vwap=2.5), NOON_ET)
    assert not ok and "< min $5" in reason


def test_entry_price_band_passes_inside():
    ok, _ = evaluate_entry(params(entry={"min_price": 1, "max_price": 10}), cand(price=6.0, vwap=5.5), NOON_ET)
    assert ok


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


# ---- optional MACD entry filter (off by default) ----

def test_entry_macd_off_ignores_signal_even_when_bearish():
    # Flag absent → MACD is never consulted, so a bearish/unknown signal is moot.
    ok, _ = evaluate_entry(params(), cand(macd_bullish=False), NOON_ET)
    assert ok


def test_entry_macd_required_passes_when_bullish():
    p = params(entry={"require_macd_bullish": True})
    ok, reason = evaluate_entry(p, cand(macd_bullish=True), NOON_ET)
    assert ok and "MACD bullish" in reason


def test_entry_macd_required_blocks_when_bearish():
    p = params(entry={"require_macd_bullish": True})
    ok, reason = evaluate_entry(p, cand(macd_bullish=False), NOON_ET)
    assert not ok and "MACD not bullish" in reason and "bearish" in reason


def test_entry_macd_required_blocks_when_unknown_fail_closed():
    # None (not enough history) must fail CLOSED — an unproven signal is not a go.
    p = params(entry={"require_macd_bullish": True})
    ok, reason = evaluate_entry(p, cand(macd_bullish=None), NOON_ET)
    assert not ok and "MACD not bullish" in reason and "history" in reason


# ---- optional MACD exit signal (off by default) ----

def test_exit_macd_off_never_fires():
    # Flag absent → a bearish MACD does nothing on its own.
    should, _ = evaluate_exit(
        params(), True, 100.0, YESTERDAY, 105.0, 104.5, None, NOW, False, macd_bullish=False
    )
    assert not should


def test_exit_macd_bearish_triggers_exit():
    p = params(exit={"exit_on_macd_bearish": True})
    should, reason = evaluate_exit(
        p, True, 100.0, YESTERDAY, 105.0, 104.5, None, NOW, False, macd_bullish=False
    )
    assert should and reason.startswith("MACD turned bearish")


def test_exit_macd_bullish_or_unknown_does_not_exit():
    p = params(exit={"exit_on_macd_bearish": True})
    for flag in (True, None):
        should, _ = evaluate_exit(
            p, True, 100.0, YESTERDAY, 105.0, 104.5, None, NOW, False, macd_bullish=flag
        )
        assert not should


def test_exit_macd_suppressed_same_day_in_swing_mode():
    # Soft exits (incl. MACD) wait until the day after entry when swinging.
    same_day_entry = NOW - timedelta(hours=2)
    p = params(exit={"exit_on_macd_bearish": True})
    should, _ = evaluate_exit(
        p, True, 100.0, same_day_entry, 101.0, 100.5, None, NOW, False, macd_bullish=False
    )
    assert not should


def test_hard_stop_wins_over_macd_exit():
    # Price gapped through the stop AND MACD is bearish — the hard stop takes
    # priority and names the reason.
    p = params(exit={"exit_on_macd_bearish": True})
    should, reason = evaluate_exit(
        p, True, 100.0, NOW - timedelta(hours=1), 100.0, 95.9, None, NOW, False, macd_bullish=False
    )
    assert should and "stop-loss" in reason


# ---- optional RSI entry band (off by default) ----

def test_entry_rsi_off_ignores_reading():
    # No band set → RSI is never consulted, even a wild reading is moot.
    ok, _ = evaluate_entry(params(), cand(rsi=95.0), NOON_ET)
    assert ok


def test_entry_rsi_max_blocks_overbought():
    p = params(entry={"rsi_max": 70})
    ok, reason = evaluate_entry(p, cand(rsi=82.0), NOON_ET)
    assert not ok and "overbought" in reason and "82" in reason


def test_entry_rsi_max_allows_below_threshold():
    p = params(entry={"rsi_max": 70})
    ok, reason = evaluate_entry(p, cand(rsi=55.0), NOON_ET)
    assert ok and "RSI 55" in reason


def test_entry_rsi_min_blocks_below_floor():
    p = params(entry={"rsi_min": 40})
    ok, reason = evaluate_entry(p, cand(rsi=30.0), NOON_ET)
    assert not ok and "< min 40" in reason


def test_entry_rsi_band_fail_closed_when_unknown():
    # A set band with no RSI reading (too little history) must fail CLOSED.
    p = params(entry={"rsi_max": 70})
    ok, reason = evaluate_entry(p, cand(rsi=None), NOON_ET)
    assert not ok and "RSI unavailable" in reason


# ---- optional RSI overbought exit (off by default) ----

def test_exit_rsi_off_never_fires():
    should, _ = evaluate_exit(
        params(), True, 100.0, YESTERDAY, 105.0, 104.5, None, NOW, False, rsi=95.0
    )
    assert not should


def test_exit_rsi_above_triggers_when_overbought():
    p = params(exit={"exit_rsi_above": 70})
    should, reason = evaluate_exit(
        p, True, 100.0, YESTERDAY, 105.0, 104.5, None, NOW, False, rsi=78.0
    )
    assert should and "78" in reason and "overbought" in reason


def test_exit_rsi_below_threshold_or_unknown_holds():
    p = params(exit={"exit_rsi_above": 70})
    for reading in (65.0, None):
        should, _ = evaluate_exit(
            p, True, 100.0, YESTERDAY, 105.0, 104.5, None, NOW, False, rsi=reading
        )
        assert not should


def test_exit_rsi_suppressed_same_day_in_swing_mode():
    # RSI exit is a soft exit — waits until the day after entry when swinging.
    same_day_entry = NOW - timedelta(hours=2)
    p = params(exit={"exit_rsi_above": 70})
    should, _ = evaluate_exit(
        p, True, 100.0, same_day_entry, 101.0, 100.5, None, NOW, False, rsi=90.0
    )
    assert not should


# ---- optional market-regime exit (off by default) ----

def test_exit_regime_off_never_fires():
    should, _ = evaluate_exit(
        params(), True, 100.0, YESTERDAY, 105.0, 104.5, None, NOW, False, regime_bearish=True
    )
    assert not should


def test_exit_regime_bear_triggers_when_on_and_bearish():
    p = params(exit={"exit_on_regime_bear": True})
    should, reason = evaluate_exit(
        p, True, 100.0, YESTERDAY, 105.0, 104.5, None, NOW, False, regime_bearish=True
    )
    assert should and "regime bearish" in reason and "200-day" in reason


def test_exit_regime_bull_or_unknown_does_not_fire():
    # regime_bearish=False covers both "SPY above its 200-day" and "unknown"
    # (the caller passes False when the regime can't be determined — fail-safe).
    p = params(exit={"exit_on_regime_bear": True})
    should, _ = evaluate_exit(
        p, True, 100.0, YESTERDAY, 105.0, 104.5, None, NOW, False, regime_bearish=False
    )
    assert not should


def test_hard_stop_wins_over_regime_exit():
    p = params(exit={"exit_on_regime_bear": True})
    should, reason = evaluate_exit(
        p, True, 100.0, NOW - timedelta(hours=1), 100.0, 95.9, None, NOW, False, regime_bearish=True
    )
    assert should and "stop-loss" in reason


# ---- optional ATR stop (off by default) ----

def atr_params(**overrides) -> dict:
    """params() plus an atr block; stop_mult drives the ATR stop."""
    p = params(**overrides)
    p["atr"] = {"period": 14, "stop_mult": 2.0, "risk_usd": 0.0}
    return p


def test_atr_stop_off_uses_fixed_stop_even_when_atr_supplied():
    # No atr block → the fixed 4% stop applies; a 3% drop does NOT stop out.
    should, _ = evaluate_exit(
        params(), True, 100.0, YESTERDAY, 100.0, 97.0, None, NOW, False, atr_pct=1.0
    )
    assert not should  # -3% > -4% fixed stop


def test_atr_stop_triggers_at_stop_mult_times_atr():
    # ATR 1.5%, stop_mult 2 → effective stop 3.0%. A 3.1% drop stops out; the
    # fixed 4% stop alone would NOT have fired, proving the ATR stop is in effect.
    p = atr_params(exit={"stop_loss_pct": 4.0})
    p["atr"]["stop_mult"] = 2.0
    should, reason = evaluate_exit(
        p, True, 100.0, YESTERDAY, 100.0, 96.9, None, NOW, False, atr_pct=1.5
    )
    assert should and "ATR stop-loss" in reason and "2× ATR 1.50%" in reason


def test_atr_stop_holds_when_move_smaller_than_atr_stop():
    # Same effective 3.0% stop; a 2.5% drop stays in.
    p = atr_params(exit={"stop_loss_pct": 4.0})
    should, _ = evaluate_exit(
        p, True, 100.0, YESTERDAY, 100.0, 97.5, None, NOW, False, atr_pct=1.5
    )
    assert not should


def test_atr_stop_falls_back_to_fixed_when_atr_unavailable():
    # stop_mult set but atr_pct None (fetch blip / too little history) → the fixed
    # 4% stop still guards the position: a 4.1% drop stops out.
    p = atr_params(exit={"stop_loss_pct": 4.0})
    should, reason = evaluate_exit(
        p, True, 100.0, YESTERDAY, 100.0, 95.9, None, NOW, False, atr_pct=None
    )
    assert should and "stop-loss" in reason and "ATR" not in reason


# ---- ATR position sizing math ----

def test_atr_sizing_off_returns_fixed_size():
    from qt.services.engine import atr_position_size

    # No atr block → the fixed size, untouched.
    assert atr_position_size(params(), 200.0, 1000.0, atr_pct=2.0) == 200.0
    # risk_usd set but stop_mult 0 → still off (sizing needs the stop distance).
    p = params()
    p["atr"] = {"period": 14, "stop_mult": 0.0, "risk_usd": 50.0}
    assert atr_position_size(p, 200.0, 1000.0, atr_pct=2.0) == 200.0


def test_atr_sizing_math():
    from qt.services.engine import atr_position_size

    # risk_usd 50, stop_mult 2, atr_pct 2.5% → stop distance 5% → size 50/0.05 = 1000.
    p = params()
    p["atr"] = {"period": 14, "stop_mult": 2.0, "risk_usd": 50.0}
    assert atr_position_size(p, 200.0, 5000.0, atr_pct=2.5) == 1000.0


def test_atr_sizing_capped_at_sleeve():
    from qt.services.engine import atr_position_size

    # A very calm name (tiny atr_pct) computes a huge size; the sleeve caps it.
    p = params()
    p["atr"] = {"period": 14, "stop_mult": 1.0, "risk_usd": 50.0}
    # 50 / (1 * 0.1/100) = 50 / 0.001 = 50000, capped to the 1000 sleeve.
    assert atr_position_size(p, 200.0, 1000.0, atr_pct=0.1) == 1000.0


def test_atr_sizing_falls_back_when_atr_none():
    from qt.services.engine import atr_position_size

    p = params()
    p["atr"] = {"period": 14, "stop_mult": 2.0, "risk_usd": 50.0}
    assert atr_position_size(p, 200.0, 1000.0, atr_pct=None) == 200.0


# ---- max-hold ceiling vs the swing deferral, and crypto's missing session ----


def test_max_hold_fires_on_entry_day_even_in_swing_mode():
    """Regression: max_holding_hours used to be evaluated BELOW the swing
    early-return, so a hold cap shorter than the rest of the entry day silently
    could not fire — "max hold 2h" quietly became "some time tomorrow". A time
    limit the user set is a hard ceiling and must always be honoured."""
    p = params(exit={"max_holding_hours": 2, "take_profit_pct": 0, "trailing_stop_pct": 0})
    entry = NOW - timedelta(hours=3)  # same ET day as NOW, 3h ago
    assert entry.astimezone(ET).date() == NOW.astimezone(ET).date()
    should, reason = evaluate_exit(p, True, 100.0, entry, 100.0, 100.0, None, NOW, False)
    assert should and "max holding period" in reason


def test_max_hold_still_waits_until_the_cap_is_reached():
    p = params(exit={"max_holding_hours": 8, "take_profit_pct": 0, "trailing_stop_pct": 0})
    entry = NOW - timedelta(hours=3)
    should, _ = evaluate_exit(p, True, 100.0, entry, 100.0, 100.0, None, NOW, False)
    assert not should


def test_crypto_ignores_the_swing_deferral():
    """Crypto has no session, so there is no "entry day" to be patient through:
    the ET-midnight boundary the deferral uses is meaningless for a 24/7 asset.
    A take-profit that is due must fire on the entry day."""
    p = params(exit={"take_profit_pct": 10.0, "trailing_stop_pct": 0})
    entry = NOW - timedelta(hours=2)  # same ET day
    # Stock + swing: deferred (the historical behaviour, unchanged).
    should, _ = evaluate_exit(p, True, 100.0, entry, 111.0, 111.0, None, NOW, False)
    assert not should
    # Same inputs, crypto: the take-profit fires.
    should, reason = evaluate_exit(
        p, True, 100.0, entry, 111.0, 111.0, None, NOW, False, is_crypto=True
    )
    assert should and "take-profit" in reason


def test_crypto_hard_stops_unchanged():
    # Stops never depended on the deferral; confirm crypto keeps them.
    p = params(exit={"stop_loss_pct": 4.0})
    entry = NOW - timedelta(hours=1)
    should, reason = evaluate_exit(
        p, True, 100.0, entry, 100.0, 95.0, None, NOW, False, is_crypto=True
    )
    assert should and "stop-loss" in reason


# ---- non-fill circuit breaker ----

def test_two_non_fills_do_not_cool_off_yet():
    """The threshold has to be a real threshold. One bad tick on a symbol that
    normally fills must not sideline it."""
    ok, reason = check_rails(
        STRAT, 100.0,
        ctx(nonfill_strikes=2, last_nonfill_at=datetime.now(timezone.utc) - timedelta(seconds=30)),
    )
    assert ok, reason


def test_three_non_fills_cool_the_symbol_off():
    ok, reason = check_rails(
        STRAT, 100.0, ctx(nonfill_strikes=3, last_nonfill_at=datetime.now(timezone.utc))
    )
    assert not ok
    assert "cooling off after 3 non-fills" in reason


def test_the_cooldown_lapses_so_the_symbol_gets_retried():
    """Without this the breaker would never reopen: a symbol that stopped filling
    once could never prove it fills again."""
    ok, reason = check_rails(
        STRAT, 100.0,
        # 3 strikes = a 1h wait; this miss was 90 minutes ago.
        ctx(nonfill_strikes=3, last_nonfill_at=datetime.now(timezone.utc) - timedelta(minutes=90)),
    )
    assert ok, reason


def test_the_wait_doubles_with_each_further_miss_and_is_capped():
    assert nonfill_cooldown_hours(2) == 0.0  # below the threshold: no wait at all
    assert nonfill_cooldown_hours(3) == 1.0
    assert nonfill_cooldown_hours(4) == 2.0
    assert nonfill_cooldown_hours(5) == 4.0
    assert nonfill_cooldown_hours(99) == NONFILL_COOLDOWN_MAX_HOURS  # never unbounded


def test_a_cooling_symbol_still_reports_the_position_rail_first():
    """Ordering matters for the trace: already holding the name is the more
    useful thing to say, and it's true regardless of fills."""
    ok, reason = check_rails(
        STRAT, 100.0,
        ctx(already_open_symbol=True, nonfill_strikes=9,
            last_nonfill_at=datetime.now(timezone.utc)),
    )
    assert not ok
    assert "already open" in reason


# ---- stale-print guard (crypto) ----

def _crypto(minutes_old: float | None, **kw) -> Candidate:
    at = None if minutes_old is None else NOON_ET - timedelta(minutes=minutes_old)
    base = dict(symbol="RENDER/USD", asset_class="crypto", price=1.3749,
                change_pct=5.0, vwap=1.0, last_trade_at=at)
    base.update(kw)
    return Candidate(**base)


def test_a_crypto_pair_with_no_recent_prints_is_skipped():
    """RENDER/USD: last print frozen for 90 minutes while its bar-derived change
    kept moving, so every gate that reads the change waved it through."""
    ok, reason = evaluate_entry(params(entry={"require_above_vwap": False}), _crypto(90), NOON_ET)
    assert not ok
    assert "no trades on this pair for 90m" in reason


def test_a_freshly_trading_pair_passes():
    """The other direction — without it, blocking all crypto would also pass."""
    ok, reason = evaluate_entry(params(entry={"require_above_vwap": False}), _crypto(2), NOON_ET)
    assert ok, reason


def test_an_unknown_print_time_is_not_treated_as_stale():
    """None means the snapshot carried no latestTrade.t. Failing closed here
    would halt every crypto entry at once if Alpaca changed its payload."""
    ok, reason = evaluate_entry(params(entry={"require_above_vwap": False}), _crypto(None), NOON_ET)
    assert ok, reason


def test_stocks_are_exempt_from_the_print_age_check():
    """A stock's last trade is legitimately hours old whenever the market is
    shut — the check would block every pre-market entry."""
    stale_stock = cand(change_pct=5.0)
    stale_stock.last_trade_at = NOON_ET - timedelta(hours=14)
    ok, reason = evaluate_entry(params(), stale_stock, NOON_ET)
    assert ok, reason


def test_the_print_time_parser_handles_alpacas_real_formats():
    """Alpaca stamps crypto trades with NANOSECOND precision and an offset —
    a format that trips naive parsing. If this returns None the whole stale
    check silently becomes a no-op, which is how it would fail in production."""
    from qt.services.engine import last_trade_at_from_snapshot as parse

    nanos = parse({"latestTrade": {"t": "2026-08-02T21:42:18.499701309-04:00"}})
    assert nanos is not None
    assert nanos.astimezone(timezone.utc).hour == 1  # 21:42 ET == 01:42 UTC next day

    zulu = parse({"latestTrade": {"t": "2026-08-03T01:42:18.499701Z"}})
    assert zulu is not None and zulu.tzinfo is not None

    assert parse({}) is None                                   # no snapshot data
    assert parse({"latestTrade": {}}) is None                  # no timestamp
    assert parse({"latestTrade": {"t": "not-a-date"}}) is None  # unparseable, not a crash
