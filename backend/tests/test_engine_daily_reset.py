"""The daily risk counters — trade-rate limiter and daily-loss kill switch —
reset on the US trading-day boundary (00:00 America/New_York), NOT midnight
UTC. Midnight UTC falls at 7-8pm ET the previous evening, which for 24/7
crypto would silently reset the trade budget and loss headroom mid-session.

These tests pin the boundary across DST and, critically, across the ET evening
where the UTC date has already rolled over but the ET trading day has not.
"""

from datetime import datetime, timedelta, timezone

import pytest

from qt.models import Strategy, Trade
from qt.services.engine import ET, _daily_loss, _trading_day_start


def _utc(y, mo, d, h, mi=0):
    return datetime(y, mo, d, h, mi, tzinfo=timezone.utc)


# --- _trading_day_start: the pure ET boundary -----------------------------


def test_boundary_in_summer_is_0400_utc():
    # EDT = UTC-4. Mid-afternoon UTC on 2026-07-13 → day began 04:00 UTC.
    start = _trading_day_start(_utc(2026, 7, 13, 18))
    assert start == _utc(2026, 7, 13, 4)
    assert start.astimezone(ET).hour == 0  # it really is ET midnight


def test_boundary_in_winter_is_0500_utc():
    # EST = UTC-5. 2026-01-15 → day began 05:00 UTC.
    start = _trading_day_start(_utc(2026, 1, 15, 18))
    assert start == _utc(2026, 1, 15, 5)
    assert start.astimezone(ET).hour == 0


def test_et_evening_keeps_the_same_trading_day_after_utc_rolls_over():
    # THE BUG. 02:00 UTC on the 14th is 22:00 EDT on the 13th — still the 13th's
    # trading day. A UTC-midnight reset would have rolled to the 14th already
    # and handed a 24/7 crypto strategy a fresh trade budget mid-evening.
    start = _trading_day_start(_utc(2026, 7, 14, 2))
    assert start == _utc(2026, 7, 13, 4)  # still the 13th's ET day, not the 14th


def test_boundary_flips_exactly_at_et_midnight_not_utc_midnight():
    just_before = _trading_day_start(_utc(2026, 7, 13, 3, 59))  # 23:59 EDT on 12th
    just_after = _trading_day_start(_utc(2026, 7, 13, 4, 1))     # 00:01 EDT on 13th
    assert just_before == _utc(2026, 7, 12, 4)
    assert just_after == _utc(2026, 7, 13, 4)
    assert just_after - just_before == timedelta(days=1)


def test_utc_midnight_still_belongs_to_the_previous_trading_day():
    # 00:30 UTC is ~20:30 ET the evening before — the old UTC reset would fire
    # here; the ET boundary must not.
    start = _trading_day_start(_utc(2026, 7, 14, 0, 30))
    assert start == _utc(2026, 7, 13, 4)


def test_boundary_is_idempotent():
    now = _utc(2026, 7, 13, 18)
    once = _trading_day_start(now)
    assert _trading_day_start(once) == once  # feeding the boundary back yields itself


def test_boundary_tracks_dst_spring_forward():
    # 2026-03-08 the US springs forward (EST→EDT). Before that date the offset
    # is -5, on/after it is -4 — the boundary must move with it, not stay fixed.
    before = _trading_day_start(_utc(2026, 3, 7, 18))
    after = _trading_day_start(_utc(2026, 3, 9, 18))
    assert before == _utc(2026, 3, 7, 5)   # EST
    assert after == _utc(2026, 3, 9, 4)     # EDT


# --- _daily_loss: only counts closed losses since the ET boundary ---------


@pytest.fixture()
def clean_mode(db_session):
    """A private mode label + throwaway strategy so these trades never collide
    with other tests (and satisfy the trades→strategies foreign key)."""
    mode = "test-reset"
    strat = Strategy(name="reset-test", asset_class="crypto", params="{}")
    db_session.add(strat)
    db_session.commit()
    yield mode, strat.id
    db_session.query(Trade).filter(Trade.mode == mode).delete()
    db_session.query(Strategy).filter(Strategy.id == strat.id).delete()
    db_session.commit()


def _closed(strategy_id, mode, exit_at, pnl):
    return Trade(
        strategy_id=strategy_id, mode=mode, symbol="AAA", asset_class="crypto",
        status="closed", qty=1, notional=100, exit_at=exit_at, pnl=pnl,
    )


def test_daily_loss_ignores_losses_before_the_boundary(db_session, clean_mode):
    mode, sid = clean_mode
    day_start = _utc(2026, 7, 13, 4)  # 00:00 ET
    db_session.add_all([
        _closed(sid, mode, day_start - timedelta(hours=1), -500),  # yesterday's ET day
        _closed(sid, mode, day_start + timedelta(hours=2), -150),  # today
        _closed(sid, mode, day_start + timedelta(hours=6), -50),   # today
    ])
    db_session.commit()
    # Only the two trades on/after the boundary count → $200 lost, not $700.
    assert _daily_loss(db_session, mode, day_start) == 200.0


def test_daily_loss_nets_wins_against_losses_and_floors_at_zero(db_session, clean_mode):
    mode, sid = clean_mode
    day_start = _utc(2026, 7, 13, 4)
    db_session.add_all([
        _closed(sid, mode, day_start + timedelta(hours=1), -100),
        _closed(sid, mode, day_start + timedelta(hours=2), 300),  # net positive day
    ])
    db_session.commit()
    assert _daily_loss(db_session, mode, day_start) == 0.0  # a green day is not a loss
