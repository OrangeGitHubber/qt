"""Two different populations, two different numbers.

MEASURED. A 720-day crypto scanner replay reported "1,737 symbol-days were
checked at daily resolution", and beside it an explanation about HELD positions:
"no chance for a stop to fire". Werner read that as 1,737 days of unwatched
positions and reasonably concluded the bar cache was broken.

It wasn't. `daily_filled_days` counts the WHOLE scanner pool — every symbol-day
anywhere in the universe that fell back to a daily bar (_fill_intraday_gaps
loops over every symbol, not the held ones). The held-position fill pass, which
runs after the first replay and has no cap, had found nothing to do: the bar
cache gained zero rows on that run, because every day a position was actually
open already had 15-minute bars.

The mechanism was right and the sentence was wrong — the same shape as the
rotation checkbox that acted while invisible, and the RSI rejection that named a
band the user had never set.

`daily_filled_held_days` counts the population the caveat is actually about. A
day with nothing open costs an entry's timing; a day with a position open costs
a stop the chance to fire when it was hit. Only the second deserves a warning.
"""

import pytest

from qt.api.backtest import daily_filled_held_days


class _DS:
    """Just the attribute the counter reads."""

    def __init__(self, bars):
        self.bars = bars


def _bar(day: str, *, fill: bool):
    b = {"t": f"{day}T14:30:00Z", "c": 100.0}
    if fill:
        b["daily_fill"] = True
    return b


def test_a_daily_day_while_holding_is_counted():
    result = {"trade_list": [{"symbol": "AAA", "entry_day": "2026-06-02",
                             "exit_day": "2026-06-04"}]}
    ds = _DS({"AAA": [_bar("2026-06-03", fill=True)]})
    assert daily_filled_held_days(result, ds) == 1


def test_a_daily_day_while_holding_NOTHING_is_not():
    """THE distinction. The symbol is in the universe and fell back to a daily
    bar, but no position was open — nothing was at risk, so it must not be
    reported as a day a stop could not fire."""
    result = {"trade_list": [{"symbol": "AAA", "entry_day": "2026-06-10",
                             "exit_day": "2026-06-11"}]}
    ds = _DS({"AAA": [_bar("2026-06-03", fill=True)]})     # outside the span
    assert daily_filled_held_days(result, ds) == 0


def test_real_intraday_days_inside_a_span_are_not_counted():
    """Only STAND-INS count. A held day that genuinely had 15-minute bars is the
    good case and must not inflate the warning."""
    result = {"trade_list": [{"symbol": "AAA", "entry_day": "2026-06-02",
                             "exit_day": "2026-06-04"}]}
    ds = _DS({"AAA": [_bar("2026-06-03", fill=False)]})
    assert daily_filled_held_days(result, ds) == 0


def test_the_tag_is_what_identifies_a_stand_in_not_the_timestamp():
    """_fill_intraday_gaps stamps a stand-in with a plausible intraday time, so
    spotting them by timestamp would miss every one. The explicit flag is the
    only reliable marker."""
    result = {"trade_list": [{"symbol": "AAA", "entry_day": "2026-06-02",
                             "exit_day": "2026-06-04"}]}
    tagged = {"t": "2026-06-03T14:30:00Z", "c": 100.0, "daily_fill": True}
    assert daily_filled_held_days(result, _DS({"AAA": [tagged]})) == 1
    untagged = {"t": "2026-06-03T14:30:00Z", "c": 100.0}
    assert daily_filled_held_days(result, _DS({"AAA": [untagged]})) == 0


def test_a_still_open_position_counts_to_the_end_of_the_test():
    """An open position needed bars right up to the last replayed day, so its
    span runs to the end rather than stopping at the entry."""
    result = {
        "trade_list": [],
        "open_positions": [{"symbol": "AAA", "entry_day": "2026-06-02"}],
        "equity_days": ["2026-06-01", "2026-06-02", "2026-06-03", "2026-06-05"],
    }
    ds = _DS({"AAA": [_bar("2026-06-03", fill=True), _bar("2026-06-05", fill=True)]})
    assert daily_filled_held_days(result, ds) == 2


def test_spans_merge_across_several_positions_in_one_symbol():
    result = {"trade_list": [
        {"symbol": "AAA", "entry_day": "2026-06-02", "exit_day": "2026-06-03"},
        {"symbol": "AAA", "entry_day": "2026-06-09", "exit_day": "2026-06-10"},
    ]}
    # 06-06 sits between the two positions; held_spans merges to 06-02..06-10, so
    # it counts. That is deliberate — the merged span is what the FETCH pass uses,
    # and the two numbers must describe the same population.
    ds = _DS({"AAA": [_bar("2026-06-06", fill=True)]})
    assert daily_filled_held_days(result, ds) == 1


def test_other_symbols_are_not_counted_against_a_held_one():
    result = {"trade_list": [{"symbol": "AAA", "entry_day": "2026-06-02",
                             "exit_day": "2026-06-04"}]}
    ds = _DS({"AAA": [], "BBB": [_bar("2026-06-03", fill=True)]})
    assert daily_filled_held_days(result, ds) == 0


def test_no_trades_at_all_reports_zero():
    ds = _DS({"AAA": [_bar("2026-06-03", fill=True)]})
    assert daily_filled_held_days({}, ds) == 0


@pytest.mark.parametrize("field", ["daily_filled_days", "daily_filled_held_days"])
def test_the_response_carries_both_numbers(field):
    """The UI prints one beside the other; a missing field would silently drop
    the qualifier and restore the original confusion."""
    from qt.api import backtest as api

    src = (api.__file__,)
    text = open(src[0], encoding="utf-8").read()
    assert f'result["{field}"]' in text
