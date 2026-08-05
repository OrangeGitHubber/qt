"""The trailing stop can scale with the symbol's volatility.

MEASURED on Werner's "Favorites" strategy (2026-08-04/05). Its 19 names spanned
a median daily move of 0.5% (SPY) to 3.8% (AMD) — an 8x range — against ONE
fixed `trailing_stop_pct`. At 3% the trail sat inside the noise for every name
that trended: DELL exceeded the whole trailing stop on a MEDIAN day and on 37 of
60 sessions, so a position in the stock that ultimately ran +93% could not
survive an ordinary week. At the other end SPY (0.5%) never came near it. No
single percentage fits that pool, which is the same argument that already gave
the HARD stop its `stop_mult` — the trail simply never got the same treatment.

`atr.trail_mult` mirrors `atr.stop_mult` exactly: 0 = off (use the fixed
percentage), > 0 = the trail becomes trail_mult x ATR% off the high water mark,
and an unavailable atr_pct falls back rather than going stopless.

The gate is the part worth testing hardest. Every caller that FETCHES daily bars
asked `_atr_stop_enabled`, so a strategy configured with only the trail would
have got `atr_pct=None`, fallen silently back to the fixed percentage, and shown
a switched-on feature doing nothing at all.
"""

from datetime import datetime, timedelta, timezone

import pytest

from qt.services import engine
from qt.services.backtest import _atr_on
from qt.services.engine import evaluate_exit

UTC = timezone.utc
ENTRY_AT = datetime(2026, 6, 1, 14, 0, tzinfo=UTC)
NOW = ENTRY_AT + timedelta(days=3)


def _params(trail_pct: float, *, trail_mult: float = 0, stop_mult: float = 0) -> dict:
    return {
        "entry": {},
        "exit": {"trailing_stop_pct": trail_pct, "stop_loss_pct": 50.0},
        "atr": {"period": 14, "stop_mult": stop_mult, "trail_mult": trail_mult},
    }


def _exit(params: dict, *, high_water: float, price: float, atr_pct=None):
    """A drop from `high_water` to `price`, with no other exit in reach."""
    return evaluate_exit(
        params, False, 100.0, ENTRY_AT, high_water, price, None, NOW, False,
        atr_pct=atr_pct, bar_high=high_water, bar_low=price,
    )


# ---------------------------------------------------------------------------
# The measured case: a volatile name keeps its position through normal noise.
# ---------------------------------------------------------------------------

def test_a_volatile_name_gets_a_wider_trail_and_holds():
    """DELL's shape: ~3.5% daily move against a 3% fixed trail. At 3x ATR the
    trail is 10.5%, so a 6% pullback — routine for it — is no longer an exit."""
    fixed = _exit(_params(3.0), high_water=120.0, price=112.8)
    assert fixed[0] is True, "the fixed 3% trail should fire on a 6% drop"

    atr = _exit(_params(3.0, trail_mult=3.0), high_water=120.0, price=112.8, atr_pct=3.5)
    assert atr[0] is False, atr[1]


def test_a_calm_name_gets_a_tighter_trail_and_sells():
    """The mirror, and the reason this is not just 'make the number bigger':
    SPY's 0.5% ATR at 3x is a 1.5% trail, which fires where the fixed 3% would
    have sat through the move."""
    fixed = _exit(_params(3.0), high_water=100.0, price=98.0)
    assert fixed[0] is False, "a 2% drop is inside a 3% fixed trail"

    atr = _exit(_params(3.0, trail_mult=3.0), high_water=100.0, price=98.0, atr_pct=0.5)
    assert atr[0] is True, atr[1]


def test_it_books_the_fill_at_the_ATR_trail_level():
    """The exit PRICE, not just the decision. `_trigger` fills at the stop level
    rather than the bar's close, so a level computed from the fixed percentage
    would misprice every ATR-trail exit while still exiting on the right bar —
    invisible in the verdict and wrong in the P&L. A mutation that swapped
    `effective_trail` for `trailing` here survived the whole first suite."""
    out: dict = {}
    fired, _ = evaluate_exit(
        _params(3.0, trail_mult=3.0), False, 100.0, ENTRY_AT, 100.0, 98.0, None,
        NOW, False, atr_pct=0.5, bar_high=100.0, bar_low=98.0, out=out,
    )
    assert fired
    # 3 x 0.50% = a 1.5% trail off a high of 100 -> 98.50 (the fixed 3% would
    # have booked 97.00, a full 1.5% of phantom loss on every exit).
    assert out["exit_price"] == pytest.approx(98.5), out


def test_the_reason_names_actual_threshold_and_multiplier():
    """Same standard as its neighbours: never make the reader remember their own
    setting and do the arithmetic."""
    fired, reason = _exit(
        _params(3.0, trail_mult=3.0), high_water=100.0, price=97.9, atr_pct=0.5
    )
    assert fired
    assert "ATR trailing stop" in reason
    assert "2.10%" in reason and "1.50%" in reason  # actual drop, threshold
    assert "3x ATR 0.50%".replace("x", "×") in reason


# ---------------------------------------------------------------------------
# Falling back, rather than going stopless or silently doing nothing.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("atr_pct", [None, 0.0])
def test_without_an_atr_value_the_fixed_percentage_still_applies(atr_pct):
    """A missing ATR must never mean 'no trailing stop'."""
    fired, reason = _exit(
        _params(3.0, trail_mult=3.0), high_water=120.0, price=112.8, atr_pct=atr_pct
    )
    assert fired is True
    assert reason.startswith("trailing stop:"), reason


def test_trail_mult_zero_leaves_the_fixed_trail_untouched():
    fired, reason = _exit(_params(3.0), high_water=120.0, price=112.8, atr_pct=3.5)
    assert fired is True
    assert reason.startswith("trailing stop:"), reason


def test_the_hard_stop_still_wins_when_both_are_breached():
    """Order matters: the hard stop is checked first and must stay that way, so
    a bar that broke both is never reported as the softer exit."""
    params = _params(3.0, trail_mult=3.0, stop_mult=2.0)
    params["exit"]["stop_loss_pct"] = 5.0
    fired, reason = evaluate_exit(
        params, False, 100.0, ENTRY_AT, 120.0, 90.0, None, NOW, False,
        atr_pct=3.5, bar_high=120.0, bar_low=90.0,
    )
    assert fired and "stop-loss" in reason and "trailing" not in reason, reason


# ---------------------------------------------------------------------------
# The gate. Without these the feature is configurable and inert.
# ---------------------------------------------------------------------------

def test_the_trail_alone_makes_the_engine_fetch_an_atr():
    """`_atr_value_needed` is what every daily-bar fetch asks. If it ignored the
    trail, atr_pct would arrive as None and the trail would silently fall back."""
    assert engine._atr_value_needed({"atr": {"trail_mult": 3.0}}) is True
    assert engine._atr_value_needed({"atr": {"stop_mult": 2.0}}) is True
    assert engine._atr_value_needed({"atr": {}}) is False
    assert engine._atr_value_needed({}) is False


def test_the_trail_alone_makes_the_backtester_compute_an_atr():
    """The same gate on the replay side — and the one that would have made a
    backtest of an ATR-trail strategy disagree with live."""
    assert _atr_on({"atr": {"trail_mult": 3.0}}) is True
    assert _atr_on({"atr": {"stop_mult": 2.0}}) is True
    assert _atr_on({"atr": {"risk_usd": 100.0}}) is True
    assert _atr_on({"atr": {"trail_mult": 0, "stop_mult": 0, "risk_usd": 0}}) is False


def test_the_trail_alone_still_counts_as_a_price_triggered_exit():
    """A strategy whose ONLY trail is the ATR one must not be judged 'no
    price-triggered exit' — that gate drives warmup and resolution choices."""
    from qt.api.backtest import _has_price_triggered_exit

    only_trail = {
        "exit": {"stop_loss_pct": 0, "trailing_stop_pct": 0, "take_profit_pct": 0},
        "atr": {"trail_mult": 3.0},
    }
    assert _has_price_triggered_exit(only_trail) is True


def test_the_saved_config_keeps_the_new_field():
    """pydantic drops unknown keys, which is why ATRConfig is declared at all —
    a new field that isn't on the model would vanish on save."""
    from qt.api.strategies import StrategyParams

    p = StrategyParams(
        exit={"stop_loss_pct": 4.0}, atr={"period": 14, "trail_mult": 2.5}
    )
    assert p.atr is not None and p.atr.trail_mult == 2.5
    assert p.model_dump()["atr"]["trail_mult"] == 2.5


@pytest.mark.parametrize("bad", [-1.0, 21.0])
def test_the_multiplier_is_bounded_like_its_neighbour(bad):
    from qt.api.strategies import ATRConfig

    with pytest.raises(ValueError):
        ATRConfig(trail_mult=bad)
