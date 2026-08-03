"""A 3.5-hour crypto segment reported 6 live buys and 0 replayed ones, blaming
the strategy's rules. The replay had not evaluated a single bar: it was handed
daily bars for a sub-day window, and no history before the window to measure a
day-gain against. Three separate faults, pinned separately here.
"""

from qt.api.backtest import BASELINE_WARMUP_DAYS, warmup_days_for
from qt.api.fidelity import MIN_HOURS_FOR_DAILY_REPLAY, _timeframe_for

PLAIN = {"entry": {"min_day_gain_pct": 0.2}, "exit": {"stop_loss_pct": 1}}
MACD = {
    "entry": {"min_day_gain_pct": 0.2, "require_macd_bullish": True},
    "exit": {"stop_loss_pct": 1},
    "macd": {"fast": 12, "slow": 26, "signal": 9},
}
BIG_ATR = {
    "entry": {"min_day_gain_pct": 0.2},
    "exit": {"stop_loss_pct": 1},
    "atr": {"period": 90, "stop_mult": 2, "risk_usd": 0},
}


# ---- 1. resolution must consider the window's LENGTH, not just the rules ----

def test_a_sub_day_window_is_not_replayed_on_daily_bars():
    """The reported case: 23:18 → 02:48 UTC, 3.5 hours, replayed as 1Day."""
    assert _timeframe_for(PLAIN, window_hours=3.5) == "15Min"


def test_a_long_window_still_uses_daily_bars():
    """Without this, everything would silently become an intraday replay — far
    heavier, and not what a multi-week comparison wants."""
    assert _timeframe_for(PLAIN, window_hours=24 * 30) == "1Day"


def test_the_length_override_leaves_daily_signal_strategies_alone():
    """Replaying a MACD strategy intraday computes MACD off intraday closes,
    which is a worse distortion than the one being fixed.

    Needs a daily-signal strategy with NO price-triggered exit: with a stop it
    is already a mixed-resolution replay (daily indicators, intraday bars),
    which is correct and has nothing to do with this override."""
    macd_no_stop = {
        "entry": {"min_day_gain_pct": 0.2, "require_macd_bullish": True},
        "exit": {"stop_loss_pct": 0, "trailing_stop_pct": 0, "take_profit_pct": 0,
                 "exit_on_macd_bearish": True},
        "macd": {"fast": 12, "slow": 26, "signal": 9},
    }
    assert _timeframe_for(macd_no_stop, window_hours=3.5) == "1Day"


def test_a_macd_strategy_with_a_stop_is_mixed_resolution_regardless():
    """Guards the branch above it: if this ever stopped being 15Min, the test
    above would be passing for the wrong reason."""
    assert _timeframe_for(MACD, window_hours=3.5) == "15Min"


def test_an_unknown_window_length_behaves_as_before():
    assert _timeframe_for(PLAIN) == "1Day"


# ---- 2. warm-up comes from the strategy's own indicator settings ----

def test_a_strategy_with_no_indicators_still_gets_a_baseline():
    """The fault that produced zero evaluated bars: no MACD/RSI/ATR meant no
    warm-up at all, so the first bar had nothing to measure a day-gain against."""
    assert warmup_days_for(PLAIN, "crypto") == BASELINE_WARMUP_DAYS["crypto"]
    assert warmup_days_for(PLAIN, "stock") == BASELINE_WARMUP_DAYS["stock"]


def test_macd_gets_the_same_lookback_the_live_engine_uses():
    """Fidelity's whole premise: same inputs both sides. The live engine reads
    MACD off a 120-day daily window, so the replay must too."""
    from qt.services.engine import MACD_LOOKBACK_DAYS

    assert warmup_days_for(MACD, "stock") == MACD_LOOKBACK_DAYS


def test_a_long_atr_period_widens_the_warmup_beyond_macds():
    """A 90-period ATR needs more than 120 calendar days; a flat constant would
    leave it undefined for the start of the window and drop those entries."""
    assert warmup_days_for(BIG_ATR, "stock") > warmup_days_for(MACD, "stock")


def test_the_baseline_is_a_floor_not_a_replacement():
    """max(), not either/or — an indicator strategy must not lose its lookback."""
    assert warmup_days_for(MACD, "crypto") > BASELINE_WARMUP_DAYS["crypto"]


# ---- 3. the report must not blame the rules for bars it never looked at ----

def test_a_window_with_no_baseline_says_so_instead_of_blaming_the_rules():
    """The report said "No bars satisfied all entry conditions simultaneously"
    for a replay that evaluated nothing. That sends you tuning rules that were
    never consulted."""
    from datetime import datetime, timedelta

    from qt.services.backtest import run_backtest
    from qt.services.engine import RISK_DEFAULTS

    strategy = {
        "asset_class": "crypto", "swing_mode": False, "sizing_usd": 100.0,
        "sleeve_usd": 1000.0, "max_positions": 1,
        "params": {"entry": {"min_day_gain_pct": 0.2, "require_above_vwap": False},
                   "exit": {"stop_loss_pct": 1}},
    }
    # Three hours of bars and nothing before them — exactly the reported segment.
    # Crypto measures its day-gain against the close 24h back, so every bar here
    # is unusable and silently skipped.
    t0 = datetime(2026, 8, 2, 23, 18)
    series = [
        {"t": (t0 + timedelta(minutes=15 * i)).strftime("%Y-%m-%dT%H:%M:%SZ"),
         "c": 100 + i, "v": 1000, "vw": 100 + i}
        for i in range(14)
    ]
    result = run_backtest(
        strategy, {"ADA/USD": series}, dict(RISK_DEFAULTS),
        starting_cash=1000, spread_pct=0, market="crypto",
    )

    assert result["trades"] == 0
    summary = (result.get("diagnosis") or {}).get("summary") or ""
    assert "could not evaluate a single bar" in summary
    assert "satisfied all entry conditions" not in summary
