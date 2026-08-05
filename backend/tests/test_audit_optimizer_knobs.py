"""What the optimizer is allowed to tune, after the 2026-08-05 exit rules.

Three faults, all introduced the same day the new rules were:

  1. `trailing_stop_pct` was still searched when the ATR TRAIL is on. The trail
     is then trail_mult x ATR% and the fixed percentage is never read
     (engine.evaluate_exit), so the search burnt a whole dimension of the grid
     proving a knob does nothing — and reported a "best" trailing stop that had
     no effect on any result. The same rule already existed for the hard stop.
  2. `atr_trail_mult` itself could not be searched at all, so the knob that
     actually governs the trail was invisible to the optimizer.
  3. `rsi_cross_above` could not be searched, which meant the DEFINING setting of
     a dip-buying strategy — the level it crosses up through — was the one thing
     a parameter search could not tune.

The rule these follow is the module's own: tune the factors you are actually
using. An inert knob is not one of them.
"""

import pytest

from qt.services.optimizer import _active_param_space, _apply_combo


def _space(params: dict) -> dict:
    return _active_param_space({"params": params}, 0.25)


ATR_TRAIL = {
    "entry": {"min_day_gain_pct": 1.0},
    "exit": {"trailing_stop_pct": 10.0, "stop_loss_pct": 4.0},
    "atr": {"period": 14, "stop_mult": 2.5, "trail_mult": 2.5},
}
FIXED_ONLY = {
    "entry": {"min_day_gain_pct": 1.0},
    "exit": {"trailing_stop_pct": 10.0, "stop_loss_pct": 4.0},
}


def test_the_fixed_trail_is_not_searched_when_the_atr_trail_is_on():
    """THE fault. evaluate_exit never reads trailing_stop_pct while trail_mult is
    set, so searching it is spending iterations on a knob with no effect."""
    space = _space(ATR_TRAIL)
    assert "atr_trail_mult" in space
    assert "trailing_stop_pct" not in space


def test_the_fixed_trail_IS_searched_when_the_atr_trail_is_off():
    """The control — dropping it unconditionally would be a worse bug than the
    one being fixed, because then nothing would tune the trail at all."""
    space = _space(FIXED_ONLY)
    assert "trailing_stop_pct" in space
    assert "atr_trail_mult" not in space


def test_the_fixed_hard_stop_is_still_dropped_for_the_atr_stop():
    """Pre-existing behaviour that must not regress while its sibling is added."""
    assert "stop_loss_pct" not in _space(ATR_TRAIL)
    assert "stop_loss_pct" in _space(FIXED_ONLY)


def test_the_rsi_crossing_level_can_be_tuned():
    """It is the defining knob of a dip-buying strategy and was unsearchable."""
    space = _space({"entry": {"rsi_cross_above": 35}, "exit": {"stop_loss_pct": 4}})
    assert "rsi_cross_above" in space
    assert len(space["rsi_cross_above"]) > 1
    assert 35 in space["rsi_cross_above"], "the current value must be among those tried"


def test_the_rsi_floor_exit_can_be_tuned():
    space = _space({"entry": {}, "exit": {"stop_loss_pct": 4, "exit_rsi_below": 45}})
    assert "exit_rsi_below" in space


@pytest.mark.parametrize("key,value", [("rsi_cross_above", 0), ("exit_rsi_below", 0),
                                       ("atr_trail_mult", 0)])
def test_a_switched_off_knob_is_still_never_searched(key, value):
    """The module's rule: a search tunes what you use, it does not turn rules ON.
    Zero has no meaningful percentage step and guessing an anchor would invent a
    rule the user did not ask for."""
    params = {"entry": {"min_day_gain_pct": 1.0, key: value},
              "exit": {"stop_loss_pct": 4.0, key: value},
              "atr": {"period": 14, key[4:] if key.startswith("atr_") else "x": value}}
    assert key not in _space(params)


def test_each_new_knob_is_written_back_where_the_engine_reads_it():
    """A knob the search can vary but cannot APPLY is worse than one it ignores:
    every combination would score identically and the winner would be noise."""
    out = _apply_combo(
        {"params": {"entry": {}, "exit": {}, "atr": {"period": 14, "trail_mult": 2.0}}},
        {"atr_trail_mult": 4.0, "rsi_cross_above": 42.0, "exit_rsi_below": 25.0},
    )
    assert out["params"]["atr"]["trail_mult"] == 4.0
    assert out["params"]["entry"]["rsi_cross_above"] == 42.0      # ENTRY block
    assert out["params"]["exit"]["exit_rsi_below"] == 25.0        # EXIT block


def test_the_atr_stop_and_trail_do_not_overwrite_each_other():
    """Both route into the same params block by name, so a combo carrying both
    must land as two distinct fields."""
    out = _apply_combo(
        {"params": {"entry": {}, "exit": {}, "atr": {"period": 14}}},
        {"atr_stop_mult": 3.0, "atr_trail_mult": 5.0},
    )
    assert out["params"]["atr"]["stop_mult"] == 3.0
    assert out["params"]["atr"]["trail_mult"] == 5.0
