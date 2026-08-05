"""RSI as a TURN, not a level: cross up to enter, roll over to exit.

WHY. Strategy 29 ranked by RSI descending — the most overbought names first —
then required a 1% up-day, above VWAP, and MACD already bullish. 17 trades,
11.8% win rate, and 6 of them exited on the HARD stop, meaning price fell below
entry without ever setting a high water mark. A third of entries went straight
down. Every filter in that stack asks "how far has this run" and none asks
"which way is it going now".

A static band cannot fix it either: `rsi_min: 30` admits a stock sitting at 32
and rotting there for six weeks. `rsi_cross_above: 30` admits it only in the
window where selling pressure actually broke.

  ENTRY  rsi_cross_above    — at/below the level `span` bars ago, above it now.
  EXIT   exit_rsi_below     — RSI drops through a floor.
  EXIT   exit_rsi_falling   — RSI turns down at all, wherever it is.

Both directions read the same two numbers (`rsi`, `rsi_prev`), which is why the
whole feature costs one extra scalar rather than a series on every bar.
"""

from datetime import datetime, timedelta, timezone

import pytest

from qt.services import stats
from qt.services.backtest import _rsi_on
from qt.services.engine import Candidate, evaluate_entry, evaluate_exit

UTC = timezone.utc
NOW = datetime(2026, 6, 10, 14, 0, tzinfo=UTC)
ENTRY_AT = NOW - timedelta(days=5)


def _cand(rsi, rsi_prev, *, change=2.0) -> Candidate:
    return Candidate(
        symbol="AAA", asset_class="stock", price=100.0, change_pct=change,
        vwap=None, rsi=rsi, rsi_prev=rsi_prev,
    )


def _entry(params: dict, cand: Candidate):
    return evaluate_entry(params, cand, NOW.astimezone(UTC))


def _params(**entry) -> dict:
    return {"entry": {"min_day_gain_pct": 1.0, **entry}, "exit": {}}


# ───────────────────────────── entry: the cross ─────────────────────────────

def test_a_genuine_cross_up_is_taken():
    ok, reason = _entry(_params(rsi_cross_above=30), _cand(34.0, 27.0))
    assert ok, reason
    assert "crossed up through 30" in reason
    assert "27 → 34" in reason


def test_still_below_the_level_is_refused():
    ok, reason = _entry(_params(rsi_cross_above=30), _cand(28.0, 22.0))
    assert not ok
    assert "no cross up yet" in reason


def test_already_above_for_a_while_is_refused():
    """THE distinction from a static band. RSI 55 having been 52 three bars ago
    passes `rsi_min: 30` every day for weeks. It is not a crossing and must be
    refused, or this setting is just a slower band."""
    ok, reason = _entry(_params(rsi_cross_above=30), _cand(55.0, 52.0))
    assert not ok
    assert "the cross is not recent" in reason


def test_exactly_at_the_level_has_not_crossed_yet():
    """`<=` on both sides: at the threshold is not through it."""
    ok, _ = _entry(_params(rsi_cross_above=30), _cand(30.0, 25.0))
    assert not ok


def test_a_missing_reading_blocks_rather_than_trades_blind():
    """Fail-closed, like MACD and the band. An unknown RSI is not a green light."""
    for now_, prev in ((None, 25.0), (34.0, None), (None, None)):
        ok, reason = _entry(_params(rsi_cross_above=30), _cand(now_, prev))
        assert not ok, (now_, prev)
        assert "RSI unavailable" in reason


def test_off_by_default_changes_nothing():
    ok, reason = _entry(_params(), _cand(None, None))
    assert ok, reason
    assert "RSI" not in reason


# ───────────────────────────── exits: the roll-over ─────────────────────────

def _exit(exit_rules: dict, rsi=None, rsi_prev=None):
    return evaluate_exit(
        {"entry": {}, "exit": {"stop_loss_pct": 50.0, **exit_rules}},
        False, 100.0, ENTRY_AT, 100.0, 100.0, None, NOW, False,
        rsi=rsi, rsi_prev=rsi_prev,
    )


def test_dropping_through_the_floor_sells():
    fired, reason = _exit({"exit_rsi_below": 45}, rsi=41.0, rsi_prev=52.0)
    assert fired
    assert "41" in reason and "45" in reason


def test_above_the_floor_holds():
    fired, _ = _exit({"exit_rsi_below": 45}, rsi=46.0, rsi_prev=52.0)
    assert not fired


def test_falling_rsi_sells_even_while_high():
    """The point of the slope exit: 68 → 61 is still a 'good' RSI by any band,
    and is exactly the roll-over you want to leave on."""
    fired, reason = _exit({"exit_rsi_falling": True}, rsi=61.0, rsi_prev=68.0)
    assert fired
    assert "68 → 61" in reason


def test_rising_rsi_holds():
    fired, _ = _exit({"exit_rsi_falling": True}, rsi=68.0, rsi_prev=61.0)
    assert not fired


def test_flat_rsi_is_not_falling():
    fired, _ = _exit({"exit_rsi_falling": True}, rsi=61.0, rsi_prev=61.0)
    assert not fired


@pytest.mark.parametrize("rules", [{"exit_rsi_below": 45}, {"exit_rsi_falling": True}])
def test_a_missing_reading_never_forces_a_sale(rules):
    """Opposite fail-safe from entry, and deliberately so: not knowing must never
    SELL a position — a fetch blip would liquidate the book."""
    assert not _exit(rules, rsi=None, rsi_prev=None)[0]
    assert not _exit(rules, rsi=None, rsi_prev=68.0)[0]
    assert not _exit({"exit_rsi_falling": True}, rsi=61.0, rsi_prev=None)[0]


# ───────────────────────────── the gates ─────────────────────────────

@pytest.mark.parametrize("params", [
    {"entry": {"rsi_cross_above": 30}, "exit": {}},
    {"entry": {}, "exit": {"exit_rsi_below": 45}},
    {"entry": {}, "exit": {"exit_rsi_falling": True}},
])
def test_each_new_rule_makes_the_replay_compute_rsi(params):
    """`_rsi_on` decides whether RSI is annotated onto the bars AT ALL. A rule
    missing from it is a rule that silently never fires in a backtest — the same
    silent-no-op class as the ATR trail's fetch gate."""
    assert _rsi_on(params) is True


def test_no_rsi_rule_still_means_no_rsi_work():
    assert _rsi_on({"entry": {"min_day_gain_pct": 1}, "exit": {"stop_loss_pct": 4}}) is False


# ───────────────────────────── rsi_back ─────────────────────────────

def test_rsi_back_reads_the_earlier_value():
    """It must equal the RSI of the series with the last `span` bars removed —
    that is the definition, and the whole feature rests on it."""
    bars = [{"c": 100.0 + (i % 7) * 2 - (i % 3)} for i in range(60)]
    assert stats.rsi_back(bars, 14, 3) == pytest.approx(stats.rsi(bars[:-3], 14))


def test_rsi_back_is_none_without_enough_history():
    assert stats.rsi_back([{"c": 100.0}] * 3, 14, 3) is None
    assert stats.rsi_back([], 14, 3) is None


def test_rsi_back_refuses_a_nonpositive_span():
    """A span of 0 would compare RSI with itself and report 'never falling'; a
    negative one slices from the FRONT (bars[:-(-3)] == bars[:3]) and answers
    with the oldest data in the series instead of the newest.

    Tested at period=2 on purpose. At the real period of 14 the explicit guard is
    redundant — a bad span leaves a slice too short for RSI, so it returns None
    either way, and removing the guard survived mutation. A small period is where
    the two behaviours actually differ, which makes the guard testable instead of
    decorative."""
    bars = [{"c": 100.0 + i} for i in range(60)]
    assert stats.rsi_back(bars, 2, 0) is None
    assert stats.rsi_back(bars, 2, -3) is None
    # …and the slice it would otherwise have used really does yield a number,
    # so this is a guard against a wrong answer and not against None.
    assert stats.rsi(bars[:3], 2) is not None


def test_the_live_exit_gate_covers_every_rsi_exit():
    """`_exit_rsi_enabled` is what the LIVE tick asks before fetching daily bars
    for an open position. A rule missing from it gets rsi=None forever and never
    fires in production — while the replay, whose gate is `_rsi_on`, fires it
    happily. That divergence is invisible until a fidelity report catches it.

    Mutation-found: dropping exit_rsi_falling from this gate passed the whole
    suite, because every other test here went through the backtester's gate."""
    from qt.services.engine import _entry_rsi_enabled, _exit_rsi_enabled

    assert _exit_rsi_enabled({"exit": {"exit_rsi_above": 70}}) is True
    assert _exit_rsi_enabled({"exit": {"exit_rsi_below": 45}}) is True
    assert _exit_rsi_enabled({"exit": {"exit_rsi_falling": True}}) is True
    assert _exit_rsi_enabled({"exit": {}}) is False
    assert _exit_rsi_enabled({}) is False

    assert _entry_rsi_enabled({"entry": {"rsi_cross_above": 30}}) is True
    assert _entry_rsi_enabled({"entry": {"rsi_min": 30}}) is True
    assert _entry_rsi_enabled({"entry": {}}) is False
