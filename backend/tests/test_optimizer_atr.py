"""The optimizer searches the ATR stop when the strategy uses one.

The rule this file protects has two halves, and the second is the one that bit:
when a strategy sets an ATR stop, stop_loss_pct is INERT — evaluate_exit replaces
it with stop_mult x ATR%. So a search that tunes stop_loss_pct on such a strategy
spends its whole budget on a knob that changes nothing, and prints a confident
"best stop-loss" beside the values that do matter.
"""

from qt.services.optimizer import _active_param_space, _apply_combo


def _strategy(**over) -> dict:
    params = {
        "entry": {"min_day_gain_pct": 2, "require_above_vwap": False},
        "exit": {"trailing_stop_pct": 5, "stop_loss_pct": 4, "take_profit_pct": 0},
    }
    params.update(over)
    return {"asset_class": "crypto", "swing_mode": False, "sizing_usd": 500,
            "sleeve_usd": 500, "max_positions": 1, "params": params}


def test_a_plain_strategy_searches_the_fixed_stop():
    """Unchanged behaviour for everything that doesn't use an ATR stop."""
    space = _active_param_space(_strategy())
    assert "stop_loss_pct" in space
    assert "atr_stop_mult" not in space


def test_an_atr_strategy_searches_the_multiplier():
    space = _active_param_space(_strategy(atr={"period": 14, "stop_mult": 1.5, "risk_usd": 0}))
    assert "atr_stop_mult" in space


def test_an_atr_strategy_stops_searching_the_knob_that_does_nothing():
    """THE point. With an ATR stop set, stop_loss_pct cannot affect a result, so
    searching it wastes iterations and reports noise as a recommendation."""
    space = _active_param_space(_strategy(atr={"period": 14, "stop_mult": 1.5, "risk_usd": 0}))
    assert "stop_loss_pct" not in space


def test_a_disabled_atr_block_is_not_treated_as_enabled():
    """stop_mult 0 means OFF — the block exists on plenty of strategies that
    never enabled it."""
    space = _active_param_space(_strategy(atr={"period": 14, "stop_mult": 0, "risk_usd": 0}))
    assert "atr_stop_mult" not in space and "stop_loss_pct" in space


def test_the_multiplier_is_written_into_the_atr_block():
    base = _strategy(atr={"period": 21, "stop_mult": 1.5, "risk_usd": 50})
    out = _apply_combo(base, {"atr_stop_mult": 2.5})
    assert out["params"]["atr"]["stop_mult"] == 2.5


def test_the_users_own_atr_period_and_risk_sizing_are_left_alone():
    """The search tunes the stop's width, not how ATR is measured or how the
    position is sized — those are deliberate choices."""
    base = _strategy(atr={"period": 21, "stop_mult": 1.5, "risk_usd": 50})
    out = _apply_combo(base, {"atr_stop_mult": 3.0})
    assert out["params"]["atr"]["period"] == 21
    assert out["params"]["atr"]["risk_usd"] == 50


def test_applying_a_combo_does_not_mutate_the_base_strategy():
    """Every iteration starts from the same base; a leak here would make each
    result depend on the order they were tried."""
    base = _strategy(atr={"period": 14, "stop_mult": 1.5, "risk_usd": 0})
    _apply_combo(base, {"atr_stop_mult": 4.0})
    assert base["params"]["atr"]["stop_mult"] == 1.5
