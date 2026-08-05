"""Keep a share of the peak GAIN, not a share of the price.

MEASURED on strategy 31. Every losing trade exited on a trail WIDER than the
entire gain it was protecting:

    PLTR   peaked +7.4%  under a 13.60% trail  ->  exited -7.3%
    GOOGL  peaked +4.2%  under an 8.24% trail  ->  exited -4.5%
    AMZN   peaked +4.7%  under a  6.99% trail  ->  exited -3.4%

A trail wider than the move cannot lock anything in. Its level sits below the
entry price, so the only thing it can ever do is book a loss — it is a second
stop-loss wearing a trailing stop's name. No choice of ATR multiple fixes that,
because the multiple is a share of PRICE and the thing being protected is a
share of the GAIN.

`exit_giveback_pct` is a share of the gain instead, which is scale-free: a
position that peaked +170% (SPCE went 2.50 -> 7.56) exits at +127%, one that
peaked +5% exits at +3.75%. Two properties follow and are tested below — it can
never convert a winner into a loser, and it puts no ceiling on the upside, which
is exactly what a take-profit does and why the two are alternatives.
"""

from datetime import datetime, timedelta, timezone

import pytest

from qt.api.strategies import ExitRules
from qt.services.engine import evaluate_exit

UTC = timezone.utc
ENTRY_AT = datetime(2026, 6, 1, 14, 0, tzinfo=UTC)
NOW = ENTRY_AT + timedelta(days=4)
ENTRY = 100.0


def _exit(rules: dict, *, high_water: float, low: float, price: float | None = None):
    out: dict = {}
    fired, reason = evaluate_exit(
        {"entry": {}, "exit": {"stop_loss_pct": 50.0, **rules}},
        False, ENTRY, ENTRY_AT, high_water, price if price is not None else low,
        None, NOW, False, bar_high=high_water, bar_low=low, out=out,
    )
    return fired, reason, out.get("exit_price")


G25 = {"exit_giveback_pct": 25}


def test_it_holds_while_the_giveback_is_small():
    """Peak +20%, so the floor is +15%. At +17% there is nothing to do."""
    fired, _, _ = _exit(G25, high_water=120.0, low=117.0)
    assert not fired


def test_it_exits_once_the_share_is_given_back():
    fired, reason, fill = _exit(G25, high_water=120.0, low=114.0)
    assert fired
    assert "gave back 25%" in reason
    assert "+20.00%" in reason and "+15.00%" in reason
    assert fill == pytest.approx(115.0), "fills AT the floor, not at the bar's low"


def test_the_tolerance_scales_with_the_move():
    """THE property. The same setting behaves completely differently on a spike
    and on a wiggle, which is what "ride it up but leave near the turn" needs."""
    # A 170% run (SPCE's shape): the floor sits at +127.5%.
    fired, _, big = _exit(G25, high_water=270.0, low=200.0)
    assert fired and big == pytest.approx(227.5)
    # A 5% wiggle: the floor sits at +3.75%.
    fired, _, small = _exit(G25, high_water=105.0, low=102.0)
    assert fired and small == pytest.approx(103.75)


def test_it_can_never_turn_a_winner_into_a_loser():
    """The fault this rule exists to fix. Whatever the give-back share, the exit
    level stays above entry — unlike a trail wider than the gain, which can only
    ever fire below it."""
    for giveback in (10, 25, 50, 90):
        for peak in (100.5, 102.0, 107.4, 250.0):
            # -10%, not -50%: a deep enough low trips the 50% hard stop FIRST and
            # the fill under test would be the stop's, not this rule's.
            _, _, fill = _exit({"exit_giveback_pct": giveback},
                               high_water=peak, low=ENTRY * 0.9)
            assert fill is not None and fill > ENTRY, (giveback, peak, fill)


def test_the_measured_pltr_trade_would_have_kept_its_gain():
    """PLTR peaked +7.4% and exited -7.3% because its trail was 13.6% wide."""
    _, _, fill = _exit(G25, high_water=107.4, low=95.0)
    assert fill == pytest.approx(105.55, abs=0.01)   # +5.55%, not -7.3%


def test_it_never_arms_on_a_position_that_is_under_water():
    """No peak gain means nothing to give back — the stop-loss owns that case,
    and arming here would put an exit ABOVE the current price."""
    fired, _, _ = _exit(G25, high_water=99.0, low=90.0)
    assert not fired


def test_it_is_off_at_zero():
    fired, _, _ = _exit({"exit_giveback_pct": 0}, high_water=200.0, low=101.0)
    assert not fired


def test_it_puts_no_ceiling_on_the_upside():
    """The difference from a take-profit, which is why they are alternatives: a
    position may run indefinitely as long as it keeps making new highs."""
    for high in (150.0, 300.0, 1000.0):
        fired, _, _ = _exit(G25, high_water=high, low=high * 0.99)
        assert not fired, high


def test_a_take_profit_still_wins_when_both_are_set():
    """Order matters and is deliberate: the take-profit is a promise about the
    exit PRICE, so a strategy carrying both should honour the promise rather
    than ride past it."""
    fired, reason, _ = _exit({"take_profit_pct": 12, "exit_giveback_pct": 25},
                             high_water=120.0, low=114.0)
    assert fired and "take-profit" in reason


@pytest.mark.parametrize("bad", [-1, 91, 100])
def test_the_share_is_bounded(bad):
    """90 is the cap: 100 would mean "exit at break-even", which the stop-loss
    already says, and a value above that is meaningless."""
    with pytest.raises(ValueError):
        ExitRules(stop_loss_pct=4, exit_giveback_pct=bad)


def test_it_is_off_by_default():
    assert ExitRules(stop_loss_pct=4).exit_giveback_pct == 0


def test_the_share_can_be_tuned_by_a_search():
    """A new exit rule that the optimizer cannot see is one you have to guess the
    setting for. 25% is a starting point, not a discovered value — and removing
    it from the searchable set passed every other test here."""
    from qt.services.optimizer import _active_param_space

    space = _active_param_space(
        {"params": {"entry": {}, "exit": {"stop_loss_pct": 4, "exit_giveback_pct": 25}}},
        0.25,
    )
    assert "exit_giveback_pct" in space
    assert 25 in space["exit_giveback_pct"], "the current value must be among those tried"
    # …and switched off stays off, like every other knob.
    off = _active_param_space(
        {"params": {"entry": {}, "exit": {"stop_loss_pct": 4, "exit_giveback_pct": 0}}}, 0.25
    )
    assert "exit_giveback_pct" not in off
