"""A strategy whose only RSI rule is the CROSSING must still get daily bars.

MEASURED. Werner built "Favorites - optimized 4 aug v2 no macd" with
rsi_cross_above set and no rsi_min/rsi_max, ran it over three months, and it took
four trades and returned 0%. The chart said "279x RSI outside the entry band" —
a band the strategy did not have.

Two faults, one cause. qt.api.backtest asked "does this use daily signals?" in
THREE separate hand-written copies, and the new direction rules were added to
none of them:

    _uses_daily_only_signals   decides whether daily bars are fetched at all
    daily_signal_names         names the signals in the error message
    _needs_warmup              decides the warm-up window

With all three answering False, no daily bars were fetched, so _annotate_rsi fell
to its single-resolution path and computed RSI off the REPLAY's own bars. At
1-hour resolution "RSI three bars ago" means three HOURS ago, so the crossing
being tested was an intraday wiggle rather than the daily turn the live engine
evaluates. The rule could not fire.

That is the third copy of this same gate bug in two days — after the ATR trail's
fetch gate and engine._exit_rsi_enabled. So the fix is not another parallel
condition: `_uses_rsi` is now the single definition and all three call it.

The reject LABEL was the second fault. `_reject_category` bucketed anything
containing "RSI" into one category labelled "RSI outside the entry band", so the
crossing's own rejections were reported as a band the user never configured.
"""

import pytest

from qt.api.backtest import (
    _needs_warmup,
    _uses_daily_only_signals,
    _uses_rsi,
    daily_signal_names,
    warmup_days_for,
)
from qt.services.backtest import _reject_category, _rsi_on

CROSS_ONLY = {
    "entry": {"min_day_gain_pct": 0, "rsi_cross_above": 30},
    "exit": {"stop_loss_pct": 4},
}
FALLING_ONLY = {"entry": {}, "exit": {"stop_loss_pct": 4, "exit_rsi_falling": True}}
BELOW_ONLY = {"entry": {}, "exit": {"stop_loss_pct": 4, "exit_rsi_below": 45}}
NO_RSI = {"entry": {"min_day_gain_pct": 1}, "exit": {"stop_loss_pct": 4}}


@pytest.mark.parametrize("params", [CROSS_ONLY, FALLING_ONLY, BELOW_ONLY])
def test_every_rsi_rule_counts_as_an_rsi_rule(params):
    assert _uses_rsi(params) is True


def test_the_old_rules_still_count():
    assert _uses_rsi({"entry": {"rsi_min": 30}, "exit": {}}) is True
    assert _uses_rsi({"entry": {"rsi_max": 70}, "exit": {}}) is True
    assert _uses_rsi({"entry": {}, "exit": {"exit_rsi_above": 70}}) is True


def test_no_rsi_rule_is_still_no_rsi_work():
    assert _uses_rsi(NO_RSI) is False
    assert _uses_rsi({}) is False


# ── the three gates that were wrong, each asserted separately ──────────────

@pytest.mark.parametrize("params", [CROSS_ONLY, FALLING_ONLY, BELOW_ONLY])
def test_the_replay_fetches_daily_bars_for_it(params):
    """THE bug. False here means no daily bars, which means RSI computed off
    hourly replay bars and a 'three bars ago' that means three hours."""
    assert _uses_daily_only_signals(params) is True


@pytest.mark.parametrize("params", [CROSS_ONLY, FALLING_ONLY, BELOW_ONLY])
def test_the_error_message_names_rsi(params):
    """When a replay can't get daily bars it explains which signals need them.
    Omitting RSI here makes that message describe the wrong strategy."""
    assert "RSI" in daily_signal_names(params)


@pytest.mark.parametrize("params", [CROSS_ONLY, FALLING_ONLY, BELOW_ONLY])
def test_it_gets_a_warm_up_window(params):
    """Wilder's RSI needs ~15 prior daily bars before it is defined at all. With
    no warm-up the rule is dead for the start of every window rather than the
    whole one — the same bug, quieter."""
    assert _needs_warmup(params) is True
    assert warmup_days_for(params, "stock") > 0


def test_the_replay_annotates_rsi_for_it():
    """The services-side gate, which was fixed earlier and must stay fixed."""
    assert _rsi_on(CROSS_ONLY) is True


# ── the mislabelled rejection ──────────────────────────────────────────────

def test_a_crossing_rejection_is_not_reported_as_a_band():
    """Werner read '279x RSI outside the entry band' on a strategy with no band
    and went looking for a setting that did not exist."""
    for reason in (
        "RSI 28 still at/below 30 — no cross up yet",
        "RSI 55 above 30, but it already was (52) — the cross is not recent",
        "RSI unavailable — rule requires an RSI crossing",
    ):
        assert _reject_category(reason) == "rsi_cross", reason


def test_a_band_rejection_is_still_reported_as_a_band():
    for reason in (
        "RSI 22 < min 30",
        "RSI 81 > max 70 (overbought)",
        "RSI unavailable — rule requires an RSI band",
    ):
        assert _reject_category(reason) == "rsi", reason


def test_the_two_labels_actually_differ():
    """Distinct categories are pointless if they print the same sentence."""
    from qt.services.backtest import _REJECT_LABELS

    assert _REJECT_LABELS["rsi_cross"] != _REJECT_LABELS["rsi"]
    assert "band" not in _REJECT_LABELS["rsi_cross"]
