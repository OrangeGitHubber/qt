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


def test_zero_is_reported_as_zero_not_as_missing():
    """take_profit_pct 0 means "off" — a real, deliberate setting. Reporting it
    as absent would render "not set" and hide that the search turned it ON."""
    b = _baseline(_strategy())
    assert b["take_profit_pct"]["value"] == 0


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


def test_an_off_grid_value_is_flagged_as_never_tested():
    """3.5 sits between the grid's 3.0 and 4.0. The search could not have tried
    it, so the UI must not imply the winner beat it."""
    s = _strategy(exit={"trailing_stop_pct": 3.5, "stop_loss_pct": 4, "take_profit_pct": 0})
    assert _baseline(s)["trailing_stop_pct"]["in_grid"] is False


def test_a_missing_knob_is_reported_as_unset_rather_than_guessed():
    """An older strategy row may simply not carry a key. "not set" is honest;
    inventing a default would fabricate the before-value of a comparison."""
    s = _strategy(exit={"trailing_stop_pct": 5, "stop_loss_pct": 4})
    b = _baseline(s)
    assert b["take_profit_pct"]["value"] is None
    assert b["take_profit_pct"]["in_grid"] is False


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
