"""The "before" half of the optimizer's before/after.

Every searched knob reports the value the strategy ALREADY had, so a result reads
as "this is what would change" rather than a bare list of numbers with no way to
tell a real proposal from one that just agreed with you.

Two things this must get right:
  - the baseline comes from the strategy the SEARCH RAN AGAINST, not from
    whatever is loaded in the UI later, and
  - it says when your current value isn't one of the values the search can draw
    from — the grid is coarse, so a setting between two grid points was never
    actually evaluated, and quietly presenting it as the losing side of a
    comparison would claim a test that never happened.
"""

from qt.services.optimizer import _active_param_space, _baseline_values


def _strategy(**over) -> dict:
    params = {
        "entry": {"min_day_gain_pct": 2, "require_above_vwap": False},
        "exit": {"trailing_stop_pct": 5, "stop_loss_pct": 4, "take_profit_pct": 0},
    }
    params.update(over)
    return {"asset_class": "crypto", "params": params}


def _baseline(strategy: dict) -> dict:
    return _baseline_values(strategy, _active_param_space(strategy))


def test_it_reports_the_strategys_current_value_for_each_knob():
    b = _baseline(_strategy())
    assert b["min_day_gain_pct"]["value"] == 2
    assert b["trailing_stop_pct"]["value"] == 5
    assert b["stop_loss_pct"]["value"] == 4


def test_a_switched_off_knob_is_not_searched_and_gets_no_baseline():
    """take_profit_pct 0 means "off", and a percentage step from zero is still
    zero — there is no grid to build. So it is not searched, and reports no
    before/after rather than a row implying it was considered."""
    b = _baseline(_strategy())
    assert "take_profit_pct" not in b


def test_it_covers_exactly_the_knobs_that_were_searched():
    """No baseline for a knob nobody searched, and none missing for one that was
    — the UI pairs these up by key."""
    s = _strategy()
    space = _active_param_space(s)
    assert set(_baseline_values(s, space)) == set(space)


def test_the_atr_multiplier_baseline_comes_from_the_atr_block():
    s = _strategy(atr={"period": 14, "stop_mult": 1.5, "risk_usd": 0})
    b = _baseline(s)
    assert b["atr_stop_mult"]["value"] == 1.5
    # ...and the fixed stop isn't searched on such a strategy, so it has no
    # baseline to show either.
    assert "stop_loss_pct" not in b


def test_an_on_grid_value_is_flagged_as_actually_tested():
    b = _baseline(_strategy())
    assert b["trailing_stop_pct"]["in_grid"] is True


def test_any_value_at_all_is_on_its_own_grid():
    """The old fixed grids held 3.0 and 4.0, so a strategy sitting at 3.5 was
    never actually evaluated at its own setting and "the winner beat your 3.5"
    was a comparison nobody had run. Anchored grids make that impossible: the
    value the strategy is on is always one of the values tried, whatever it is —
    including the odd decimals a previous search produced."""
    for odd in (3.5, 0.87, 12.345, 49.9):
        s = _strategy(exit={"trailing_stop_pct": odd, "stop_loss_pct": 4, "take_profit_pct": 0})
        b = _baseline(s)
        assert b["trailing_stop_pct"]["value"] == odd
        assert b["trailing_stop_pct"]["in_grid"] is True


def test_a_value_below_what_the_editor_allows_is_left_alone_entirely():
    """A trailing stop of 0.07% cannot be typed into the strategy editor (its
    floor is 0.5%) but an older row imported through the API could hold one. The
    search will not tune it: every neighbouring step is also below the floor, so
    there is nothing it could propose that the user could then save.

    What it must NOT do is quietly renormalize. The knob is dropped from the
    search — and therefore from the before/after panel — rather than appearing
    there with a value silently pulled up to 0.5, which would show the user a
    "current setting" their strategy does not have."""
    s = _strategy(exit={"trailing_stop_pct": 0.07, "stop_loss_pct": 4, "take_profit_pct": 0})
    assert "trailing_stop_pct" not in _active_param_space(s)
    assert "trailing_stop_pct" not in _baseline(s)
    # The strategy itself is untouched — nothing rewrote the stored value.
    assert s["params"]["exit"]["trailing_stop_pct"] == 0.07


def test_a_missing_knob_is_simply_not_searched():
    """An older strategy row may not carry a key at all. Absent reads the same as
    off — there is nothing to anchor a grid on, so it is left alone rather than
    given an invented starting value."""
    s = _strategy(exit={"trailing_stop_pct": 5, "stop_loss_pct": 4})
    assert "take_profit_pct" not in _baseline(s)


def test_the_macd_baseline_reads_the_period_not_the_toggle():
    s = _strategy(
        entry={"min_day_gain_pct": 2, "require_macd_bullish": True},
        macd={"fast": 12, "slow": 21, "signal": 9},
    )
    assert _baseline(s)["macd_slow"]["value"] == 21


def test_a_macd_strategy_that_never_set_periods_falls_back_to_the_default():
    """params["macd"] serializes as null for a strategy using stock periods; the
    baseline is the default the engine actually uses (26), not "not set"."""
    s = _strategy(entry={"min_day_gain_pct": 2, "require_macd_bullish": True}, macd=None)
    assert _baseline(s)["macd_slow"]["value"] == 26
