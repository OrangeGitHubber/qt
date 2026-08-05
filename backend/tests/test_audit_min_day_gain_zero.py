"""`min_day_gain_pct: 0` means "must not be down", not "off".

MEASURED on strategy 31 ("Favorites - optimized 4 aug v2 no macd"), built around
the RSI crossing with no minimum gain configured. Over 23 days it blocked 321
symbol-days with "day gain below the minimum" — a minimum the settings page
showed as absent, because the page hides zeros.

evaluate_entry reads:

    min_gain = entry.get("min_day_gain_pct", 0)
    if candidate.change_pct < min_gain: reject

Every neighbouring rule is guarded by `if value and …`, so 0 disables it. This
one is not, so 0 is a live threshold that rejects any stock DOWN on the day. For
a momentum strategy that is invisible. For a mean-reversion entry it is fatal: a
stock crossing up out of oversold has just stopped falling and is frequently
still red.

The fix is the FLOOR, not the meaning of 0. Redefining 0 as "off" would silently
loosen every existing strategy sitting at 0 — a live-trading behaviour change
nobody asked for. Allowing negatives adds the missing expression and changes
nothing that already works.
"""

from datetime import datetime, timezone

import pytest

from qt.api.strategies import EntryRules
from qt.services.engine import Candidate, evaluate_entry

NOW = datetime(2026, 6, 10, 14, 0, tzinfo=timezone.utc)


def _cand(change_pct: float) -> Candidate:
    return Candidate(
        symbol="AAA", asset_class="stock", price=100.0,
        change_pct=change_pct, vwap=None,
    )


def _entry(min_gain: float, change_pct: float):
    return evaluate_entry(
        {"entry": {"min_day_gain_pct": min_gain}, "exit": {}}, _cand(change_pct), NOW
    )


def test_zero_still_rejects_a_stock_that_is_down():
    """The measured behaviour. Documented here so it cannot be 'fixed' into
    meaning 'off' without someone reading why that would be a live change."""
    ok, reason = _entry(0, -1.2)
    assert not ok
    assert "day gain -1.20% < required 0%" in reason


def test_zero_accepts_flat_and_up():
    assert _entry(0, 0.0)[0] is True
    assert _entry(0, 0.4)[0] is True


def test_a_negative_minimum_accepts_a_down_day():
    """The point of the change: this is the only way to say 'today's direction
    does not matter', which is what a buy-the-dip entry needs."""
    assert _entry(-100, -6.5)[0] is True
    assert _entry(-5, -4.9)[0] is True


def test_a_negative_minimum_is_still_a_threshold():
    """Not a synonym for off — -5 must still refuse a 6% collapse, or the
    setting would be untrustworthy in the other direction."""
    ok, reason = _entry(-5, -6.1)
    assert not ok
    assert "-5%" in reason


def test_the_schema_accepts_negatives_now():
    assert EntryRules(min_day_gain_pct=-100).min_day_gain_pct == -100
    assert EntryRules(min_day_gain_pct=-2.5).min_day_gain_pct == -2.5


def test_the_schema_still_has_a_floor():
    """Widening a bound is not removing it."""
    with pytest.raises(ValueError):
        EntryRules(min_day_gain_pct=-101)
    with pytest.raises(ValueError):
        EntryRules(min_day_gain_pct=101)


def test_the_default_is_unchanged():
    """Existing strategies must be untouched by this — the whole reason the
    floor moved instead of the meaning of 0."""
    assert EntryRules().min_day_gain_pct == 3.0


def test_a_crossing_entry_survives_a_red_day_when_the_minimum_allows_it():
    """End to end on the case that started this: RSI crossed up through 30 while
    the stock is still down 1.2% on the day. With min_day_gain at 0 the crossing
    never gets evaluated; at -100 it does."""
    params = {"entry": {"min_day_gain_pct": -100, "rsi_cross_above": 30}, "exit": {}}
    cand = Candidate(
        symbol="AAA", asset_class="stock", price=100.0, change_pct=-1.2,
        vwap=None, rsi=34.0, rsi_prev=27.0,
    )
    ok, reason = evaluate_entry(params, cand, NOW)
    assert ok, reason
    assert "crossed up through 30" in reason

    blocked = {"entry": {"min_day_gain_pct": 0, "rsi_cross_above": 30}, "exit": {}}
    ok2, reason2 = evaluate_entry(blocked, cand, NOW)
    assert not ok2 and "day gain" in reason2
