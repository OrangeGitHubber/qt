"""The daily-bars window has to read the strategy's MACD periods, not a constant.

`warmup_days_for` promises history "from the strategy's OWN indicator settings
rather than one constant for everybody". That was true of ATR and false of MACD:
`_daily_lookback_days` returned a flat `MACD_LOOKBACK_DAYS` whenever ATR was off,
whatever the MACD periods said.

WHY IT MATTERS, and why it is invisible. `MACDConfig` allows `slow` up to 200
and `signal` up to 100 — about 300 completed daily bars before the signal line
exists — against 120 CALENDAR days, which is roughly 83 trading bars for a
stock. Short the fetch and the MACD is undefined across the opening stretch of
the window; an undefined MACD does not error, it drops every entry that asked
for it. The symptom is a strategy that mysteriously does not fire, and nothing
in the report points at the fetch.

The defaults must not move. 12/26/9 asks for (26+9)*2+10 = 80, under the 120
floor, so every existing MACD-only fetch stays byte-identical — which is what
makes this safe to land without re-running anything.
"""

import pytest

from qt.services.engine import MACD_LOOKBACK_DAYS, _daily_lookback_days


def _p(*, fast=12, slow=26, signal=9, atr_period=None):
    params = {"macd": {"fast": fast, "slow": slow, "signal": signal}}
    if atr_period is not None:
        params["atr"] = {"period": atr_period, "stop_mult": 2.0}
    return params


def test_the_defaults_are_unchanged():
    """THE COMPATIBILITY CLAIM. If this moves, every cached MACD-only fetch and
    every warmup-sensitive fixture in the suite moves with it."""
    assert _daily_lookback_days(_p(), want_atr=False) == MACD_LOOKBACK_DAYS


def test_no_macd_block_at_all_is_still_the_floor():
    """A strategy that never opted into MACD has no `macd` key; the readers
    default to 12/26/9 and must land on the same number."""
    assert _daily_lookback_days({}, want_atr=False) == MACD_LOOKBACK_DAYS


@pytest.mark.parametrize("slow,signal,expected", [
    (100, 50, (100 + 50) * 2 + 10),      # 310 — the case from the queue
    (200, 100, (200 + 100) * 2 + 10),    # 610 — the schema's ceiling
    (60, 20, (60 + 20) * 2 + 10),        # 170 — just past the floor
])
def test_long_periods_widen_the_window(slow, signal, expected):
    got = _daily_lookback_days(_p(slow=slow, signal=signal), want_atr=False)
    assert got == expected, (
        f"MACD {slow}/{signal} needs ~{slow + signal} bars; got {got} calendar days")


def test_the_signal_period_counts_too():
    """The signal line is an EMA *of the MACD line*, so it needs its own bars on
    top of `slow`. Sizing on `slow` alone is short by exactly `signal`."""
    only_slow = _daily_lookback_days(_p(slow=100, signal=9), want_atr=False)
    with_signal = _daily_lookback_days(_p(slow=100, signal=60), want_atr=False)
    assert with_signal > only_slow, "raising `signal` did not widen the window"
    assert with_signal - only_slow == (60 - 9) * 2


def test_the_floor_still_applies_to_short_periods():
    """A fast MACD must not SHRINK the window below the floor — 120 days is also
    what the day-gain baseline and the ranking metrics quietly rely on."""
    assert _daily_lookback_days(_p(fast=2, slow=3, signal=1),
                                want_atr=False) == MACD_LOOKBACK_DAYS


def test_atr_and_macd_take_the_larger_of_the_two():
    """Both indicators can be on at once, and the fetch has to satisfy the
    hungrier one. Whichever is larger must win in BOTH directions."""
    macd_hungry = _p(slow=200, signal=100, atr_period=14)
    assert _daily_lookback_days(macd_hungry, want_atr=True) == (200 + 100) * 2 + 10
    atr_hungry = _p(slow=26, signal=9, atr_period=100)
    assert _daily_lookback_days(atr_hungry, want_atr=True) == 100 * 2 + 10


def test_atr_is_ignored_when_it_is_switched_off():
    """`want_atr` is the caller's answer to "is any ATR feature on". A large
    period sitting in params with every multiplier at zero must not widen the
    fetch — that would be paying for history nothing reads."""
    assert _daily_lookback_days(_p(atr_period=100), want_atr=False) == MACD_LOOKBACK_DAYS


def test_warmup_days_for_passes_the_periods_through():
    """The backtest's warmup delegates here precisely so the replay's MACD sees
    the same span live gave it. A long MACD must reach `warmup_days_for` too,
    or the replay is short exactly where the live engine was not."""
    from qt.api.backtest import warmup_days_for

    params = {"entry": {"require_macd_bullish": True},
              "exit": {}, "macd": {"fast": 12, "slow": 150, "signal": 40}}
    assert warmup_days_for(params, "stock") == (150 + 40) * 2 + 10


def test_a_strategy_with_no_indicators_keeps_its_small_baseline():
    """The other side of the same guard: warmup is opt-in. A strategy using no
    daily indicator at all must not start paying for 120 days of history."""
    from qt.api.backtest import warmup_days_for

    plain = {"entry": {"min_day_gain_pct": 2.0}, "exit": {"stop_loss_pct": 5}}
    assert warmup_days_for(plain, "stock") < MACD_LOOKBACK_DAYS
