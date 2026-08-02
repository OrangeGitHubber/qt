"""Every knob is searched on a GEOMETRIC grid anchored on its current value.

The old fixed lists (2.0, 3.0, 4.0 …) were scale-blind: one step meant +50% at
the bottom and +12% near the top, so resolution was coarsest exactly where a knob
is most sensitive. They also rarely contained the value the strategy was actually
running, so the search never evaluated your config and the before/after was a
comparison nobody had run.

A relative step fixes both by construction. What has to be proven is that the
arithmetic stays inside what a strategy can legally be SAVED with, that rounding
doesn't produce duplicate or degenerate grids, and that the anchor survives
exactly.
"""

import random

import pytest

from qt.services.optimizer import (
    KNOB_BOUNDS,
    RELATIVE_STEP_DEFAULT,
    STEPS_EACH_WAY,
    _active_param_space,
    _geometric_grid,
)


def _strategy(**over) -> dict:
    params = {
        "entry": {"min_day_gain_pct": 1, "require_above_vwap": False},
        "exit": {"trailing_stop_pct": 2, "stop_loss_pct": 8, "take_profit_pct": 10},
    }
    params.update(over)
    return {"asset_class": "crypto", "params": params}


PCT = KNOB_BOUNDS["trailing_stop_pct"]


def test_the_steps_are_a_constant_percentage_of_the_previous_value():
    """1 -> 1.15 -> 1.3225 up, and the reciprocals down."""
    grid = _geometric_grid(1.0, 0.15, PCT)
    assert 1.0 in grid
    assert 1.15 in grid and 1.32 in grid          # 1.15, 1.15^2
    assert 0.87 in grid and 0.76 in grid          # 1/1.15, 1/1.15^2


def test_the_ratio_between_neighbours_is_the_same_everywhere():
    """The whole point: a step near the bottom of the range is the same
    proportional change as a step near the top. Fixed grids could not do this."""
    grid = _geometric_grid(10.0, 0.25, KNOB_BOUNDS["take_profit_pct"])
    ratios = [b / a for a, b in zip(grid, grid[1:])]
    assert all(ratio == pytest.approx(1.25, rel=0.01) for ratio in ratios)


def test_the_anchor_is_always_tested_exactly():
    """Not rounded, not approximated. This is what makes "your 3.456 vs the
    winner's 3.97" an honest comparison rather than an implied one."""
    for anchor in (3.456, 0.07, 12.345, 49.9):
        assert anchor in _geometric_grid(anchor, 0.15, PCT)


def test_the_grid_spans_a_step_in_both_directions():
    grid = _geometric_grid(10.0, 0.15, PCT)
    assert len(grid) == STEPS_EACH_WAY * 2 + 1
    assert min(grid) < 10.0 < max(grid)


def test_values_never_escape_what_a_strategy_can_be_saved_with():
    """A proposed value that the strategy schema would reject is worse than
    useless — the draft it produces can't be saved. RSI is the tight case: four
    15% steps above 90 would reach 157."""
    for key, (lo, hi, _dec) in KNOB_BOUNDS.items():
        for anchor in (lo, hi, (lo + hi) / 2):
            for step in (0.05, 0.15, 0.5):
                for v in _geometric_grid(anchor, step, KNOB_BOUNDS[key]):
                    assert lo <= v <= hi, f"{key} produced {v}"


def test_an_integer_knob_stays_integral_and_deduplicated():
    """MACD periods are whole numbers. Rounding 15% steps around a small period
    lands on the same integer more than once, which would put duplicate values in
    the grid and skew the random draw toward them."""
    grid = _geometric_grid(5, 0.15, KNOB_BOUNDS["macd_slow"])
    assert all(float(v).is_integer() for v in grid)
    assert len(grid) == len(set(grid))


def test_a_knob_at_its_ceiling_only_searches_downwards():
    """50 is the maximum trailing stop, so every step up is clipped away and the
    grid becomes one-sided rather than proposing values that can't be saved."""
    space = _active_param_space(
        _strategy(exit={"trailing_stop_pct": 50, "stop_loss_pct": 8, "take_profit_pct": 0}),
        0.05,
    )
    grid = space["trailing_stop_pct"]
    assert max(grid) == 50 and min(grid) < 50


def test_a_grid_that_collapses_to_one_value_is_dropped():
    """An integer knob at its floor, stepped finely enough that every neighbour
    rounds back onto it, yields a single value. That is not a search: leaving it
    in would list a knob in the plateau report that no iteration could vary, and
    a flat one-bar chart reads as "this setting doesn't matter" rather than "this
    setting wasn't explored".

    Reachable only below the step floor the API enforces (5%) — _geometric_grid
    is general, so the guard is tested where it can actually fire."""
    assert _geometric_grid(3, 0.001, KNOB_BOUNDS["macd_slow"]) == [3.0]
    s = _strategy(
        entry={"min_day_gain_pct": 1, "require_macd_bullish": True},
        macd={"fast": 2, "slow": 3, "signal": 9},
    )
    assert "macd_slow" not in _active_param_space(s, 0.001)


def test_a_bigger_step_reaches_further_without_adding_values():
    """The step controls RANGE, not resolution — the count is fixed by
    STEPS_EACH_WAY, so widening it doesn't quietly enlarge the search space."""
    tight = _geometric_grid(10.0, 0.10, PCT)
    wide = _geometric_grid(10.0, 0.25, PCT)
    assert len(tight) == len(wide)
    assert max(wide) > max(tight) and min(wide) < min(tight)


def test_the_default_step_is_applied_when_none_is_asked_for():
    space = _active_param_space(_strategy())
    assert space["trailing_stop_pct"] == _geometric_grid(2, RELATIVE_STEP_DEFAULT, PCT)


def test_every_searched_knob_is_one_the_strategy_actually_uses():
    """min gain 1, trailing 2, stop 8, take-profit 10 — all on. RSI and MACD are
    off, so they are absent rather than invented."""
    space = _active_param_space(_strategy())
    assert set(space) == {
        "min_day_gain_pct", "trailing_stop_pct", "stop_loss_pct", "take_profit_pct",
    }


def test_every_config_the_search_can_reach_is_one_that_could_be_saved():
    """A winner whose draft the strategy form rejects is a dead end presented as
    a recommendation. This samples the whole reachable space of a strategy using
    every searchable feature at once, across every allowed step size, and puts
    each config through the REAL strategy model."""
    from qt.api.strategies import StrategyParams
    from qt.services.optimizer import _apply_combo, _combo_is_valid

    base = {
        "params": {
            "entry": {"min_day_gain_pct": 1, "rsi_min": 40, "rsi_max": 70,
                      "require_macd_bullish": True, "require_above_vwap": False},
            "exit": {"trailing_stop_pct": 2, "stop_loss_pct": 8, "take_profit_pct": 10,
                     "exit_rsi_above": 75},
            "macd": {"fast": 12, "slow": 26, "signal": 9},
            "atr": {"stop_mult": 1.5, "period": 14, "risk_usd": 0},
        }
    }
    rng = random.Random(1)
    for step in (0.05, 0.15, 0.25, 0.5):
        space = _active_param_space(base, step)
        keys = list(space)
        for _ in range(400):
            combo = {k: rng.choice(space[k]) for k in keys}
            if not _combo_is_valid(combo):
                continue
            StrategyParams(**_apply_combo(base, combo)["params"])  # raises if invalid


def test_a_crossed_rsi_band_is_rejected_before_it_is_ever_run():
    """rsi_min and rsi_max are searched independently, so a wide step can push
    min UP past a max that stepped DOWN — an entry band with no width, which the
    strategy model refuses. Caught before evaluation, so no iteration is spent on
    it and it can never surface as the winner."""
    from qt.services.optimizer import _combo_is_valid

    assert _combo_is_valid({"rsi_min": 40, "rsi_max": 70}) is True
    assert _combo_is_valid({"rsi_min": 70, "rsi_max": 40}) is False
    assert _combo_is_valid({"rsi_min": 50, "rsi_max": 50}) is False
    # Only one of the pair searched (or neither) is always fine.
    assert _combo_is_valid({"rsi_min": 70}) is True
    assert _combo_is_valid({"trailing_stop_pct": 2}) is True


def test_the_search_itself_never_evaluates_a_crossed_rsi_band():
    """The guard has to hold inside optimize(), not just in the helper: combos
    come from BOTH the random draw and the hill-climb that sweeps neighbours
    around the leader, and either could construct a band with no width."""
    from datetime import datetime, timedelta

    from qt.services import optimizer as opt

    seen: list[dict] = []

    def fake(strategy, bars_by_symbol, risk, **kw):
        entry = strategy["params"]["entry"]
        seen.append({"rsi_min": entry.get("rsi_min"), "rsi_max": entry.get("rsi_max")})
        return {"net_pnl_pct": entry.get("rsi_min", 0), "trades": 5, "win_rate": 50,
                "max_drawdown_pct": 1, "return_on_deployed_pct": 1, "open_positions": [],
                "hold_benchmark": [0.0]}

    t0 = datetime.fromisoformat("2026-01-05T14:00:00+00:00")
    bars = {"AAA": [
        {"t": (t0 + timedelta(days=i)).strftime("%Y-%m-%dT%H:%M:%SZ"),
         "c": 100 + i, "v": 10000, "vw": 100 + i}
        for i in range(40)
    ]}
    base = {
        "asset_class": "stock", "swing_mode": False, "sizing_usd": 1000,
        "sleeve_usd": 5000, "max_positions": 3,
        "params": {
            "entry": {"min_day_gain_pct": 1, "rsi_min": 40, "rsi_max": 70,
                      "require_above_vwap": False},
            "exit": {"trailing_stop_pct": 2, "stop_loss_pct": 8, "take_profit_pct": 10},
        },
    }
    # The widest allowed step, where the two RSI grids overlap the most. The fake
    # scores higher for a bigger rsi_min, so the hill-climb is actively pushed
    # toward the crossing it must refuse.
    opt.optimize(base, bars, {}, iterations=60, seed=3, relative_step=0.5, backtest_fn=fake)

    assert seen, "the search ran nothing"
    crossed = [c for c in seen if c["rsi_min"] is not None and c["rsi_min"] >= c["rsi_max"]]
    assert not crossed, f"{len(crossed)} configs with an impossible RSI band reached the backtester"
