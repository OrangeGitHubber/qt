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

The first fix widened the FLOOR to -100 and left 0 meaning "not down", to avoid
silently loosening existing strategies. Werner then confirmed this is a dev box
and no strategy of his uses 0 as a minimum, so 0 now means OFF like every other
optional rule, and the trap is gone rather than documented.

A NEGATIVE value is still a live threshold — "down, but no worse than this" —
which is why the guard is truthiness and not `> 0`.
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


def test_zero_is_off_and_accepts_a_down_day():
    """The change. Previously 0 rejected anything red, which no other optional
    rule here does and which no value could switch off."""
    ok, reason = _entry(0, -1.2)
    assert ok, reason

    ok2, _ = _entry(0, -9.9)
    assert ok2


def test_zero_still_accepts_flat_and_up():
    assert _entry(0, 0.0)[0] is True
    assert _entry(0, 0.4)[0] is True


def test_a_positive_minimum_still_filters():
    """Off at 0 must not mean off everywhere — the momentum use case is the
    common one and has to keep working."""
    ok, reason = _entry(3, 1.4)
    assert not ok
    assert "day gain 1.40% < required 3%" in reason
    assert _entry(3, 3.1)[0] is True


def test_exactly_at_the_minimum_is_accepted():
    """"Min gain" means AT LEAST this much, so the boundary belongs to the
    trader. Swapping `<` for `<=` in the guard survived the rest of this file —
    nothing else here pins which side of the line a exact match falls on."""
    assert _entry(3, 3.0)[0] is True
    assert _entry(-5, -5.0)[0] is True


def test_a_negative_minimum_is_a_threshold_not_an_off_switch():
    """The reason the guard tests truthiness rather than `> 0`: -5 must still
    refuse a 6% collapse. Treating negatives as 'off' would make the setting
    silently useless in the direction it exists for."""
    assert _entry(-5, -4.9)[0] is True
    ok, reason = _entry(-5, -6.1)
    assert not ok
    assert "-5%" in reason


def test_the_schema_accepts_negatives():
    assert EntryRules(min_day_gain_pct=-100).min_day_gain_pct == -100
    assert EntryRules(min_day_gain_pct=-2.5).min_day_gain_pct == -2.5


def test_the_schema_still_has_a_floor():
    """Widening a bound is not removing it."""
    with pytest.raises(ValueError):
        EntryRules(min_day_gain_pct=-101)
    with pytest.raises(ValueError):
        EntryRules(min_day_gain_pct=101)


def test_the_default_is_unchanged():
    """3% still means 3% — only the meaning of ZERO moved."""
    assert EntryRules().min_day_gain_pct == 3.0


def test_a_crossing_entry_survives_a_red_day():
    """End to end on the case that started this: RSI crossed up through 30 while
    the stock is still down 1.2%. That is the normal shape of the signal, and it
    now reaches the crossing rule instead of dying at the gain check."""
    params = {"entry": {"min_day_gain_pct": 0, "rsi_cross_above": 30}, "exit": {}}
    cand = Candidate(
        symbol="AAA", asset_class="stock", price=100.0, change_pct=-1.2,
        vwap=None, rsi=34.0, rsi_prev=27.0,
    )
    ok, reason = evaluate_entry(params, cand, NOW)
    assert ok, reason
    assert "crossed up through 30" in reason
