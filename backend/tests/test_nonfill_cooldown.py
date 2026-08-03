"""The non-fill streak is read back off the journal rather than held in memory,
so a deploy can't hand a dead symbol a clean slate. These cover the counting
itself — the pure cooldown arithmetic lives in test_engine_rules.py.
"""

from datetime import datetime, timedelta, timezone

import pytest

from qt.db import session_scope
from qt.models import Strategy, Trade
from qt.services.engine import RISK_DEFAULTS, _build_rail_context

SYMBOL = "NOFILL/USD"
NOW = datetime.now(timezone.utc)


@pytest.fixture()
def strategy_row():
    with session_scope() as s:
        strat = Strategy(
            name="nonfill test", enabled=True, asset_class="crypto", universe="scanner",
            preset="custom", params='{"entry":{},"exit":{"stop_loss_pct":4}}',
            sizing_usd=100, sleeve_usd=1000, max_positions=3, swing_mode=False,
            ignore_regime=False,
        )
        s.add(strat)
        s.flush()
        sid = strat.id
    yield sid
    with session_scope() as s:
        s.query(Trade).filter(Trade.strategy_id == sid).delete()
        s.query(Strategy).filter(Strategy.id == sid).delete()


def _row(sid: int, *, status: str, reason: str, minutes_ago: int) -> Trade:
    return Trade(
        strategy_id=sid, mode="paper", symbol=SYMBOL, asset_class="crypto",
        qty=1, notional=100, status=status, entry_reason=reason,
        created_at=NOW - timedelta(minutes=minutes_ago),
    )


def _strikes(sid: int) -> tuple[int, datetime | None]:
    with session_scope() as s:
        strategy = s.get(Strategy, sid)
        ctx = _build_rail_context(
            s, "paper", strategy, SYMBOL, 10_000.0, dict(RISK_DEFAULTS), False, 0.0,
            NOW - timedelta(days=1),
        )
        return ctx.nonfill_strikes, ctx.last_nonfill_at


def test_consecutive_non_fills_are_counted(strategy_row):
    with session_scope() as s:
        for m in (3, 2, 1):
            s.add(_row(strategy_row, status="rejected", reason="but market order did not fill in 6s", minutes_ago=m))

    strikes, last = _strikes(strategy_row)
    assert strikes == 3
    # Measured from the MOST RECENT miss, or the wait would start from the wrong
    # end and a long streak would reopen immediately.
    assert last is not None
    assert abs((last - (NOW - timedelta(minutes=1))).total_seconds()) < 2


def test_a_fill_resets_the_streak(strategy_row):
    """The breaker has to reopen for real. A symbol that filled since its misses
    is not a symbol that can't fill."""
    with session_scope() as s:
        for m in (30, 29, 28):
            s.add(_row(strategy_row, status="rejected", reason="but market order did not fill in 6s", minutes_ago=m))
        s.add(_row(strategy_row, status="open", reason="bought", minutes_ago=5))

    assert _strikes(strategy_row) == (0, None)


def test_rail_rejections_neither_count_nor_reset(strategy_row):
    """Never having placed an order is not a missed fill — but it isn't evidence
    the symbol recovered either. Rail rows must be transparent to the streak."""
    with session_scope() as s:
        s.add(_row(strategy_row, status="rejected", reason="but market order did not fill in 6s", minutes_ago=10))
        s.add(_row(strategy_row, status="rejected", reason="but rail: strategy max positions reached (5)", minutes_ago=8))
        s.add(_row(strategy_row, status="rejected", reason="but market order did not fill in 6s", minutes_ago=6))

    strikes, _ = _strikes(strategy_row)
    assert strikes == 2  # the rail row in the middle is skipped, not counted


def test_the_streak_is_per_symbol(strategy_row):
    """Three misses on one pair must not sideline every other symbol. Without
    the symbol filter, RENDER/USD's streak would cool off the whole strategy."""
    with session_scope() as s:
        for m in (3, 2, 1):
            s.add(_row(strategy_row, status="rejected", reason="but market order did not fill in 6s", minutes_ago=m))

    with session_scope() as s:
        strategy = s.get(Strategy, strategy_row)
        ctx = _build_rail_context(
            s, "paper", strategy, "OTHER/USD", 10_000.0, dict(RISK_DEFAULTS), False, 0.0,
            NOW - timedelta(days=1),
        )
    assert (ctx.nonfill_strikes, ctx.last_nonfill_at) == (0, None)


def test_the_streak_is_per_mode(strategy_row):
    """Shadow mode places no orders, so it cannot produce a real non-fill —
    and must never cool off paper trading."""
    with session_scope() as s:
        for m in (3, 2, 1):
            row = _row(strategy_row, status="rejected", reason="but market order did not fill in 6s", minutes_ago=m)
            row.mode = "shadow"
            s.add(row)

    assert _strikes(strategy_row) == (0, None)


def test_the_rails_own_rejections_cannot_evict_the_evidence(strategy_row):
    """THE regression that made the breaker useless. The cooling-off rail writes
    a rejected row every cycle it blocks; at a 60s tick those filled the lookback
    window in under an hour and pushed out the non-fills that justified it, so
    the cooldown released itself after ~50 minutes and the 12h cap was
    unreachable."""
    with session_scope() as s:
        for m in (200, 199, 198):
            s.add(_row(strategy_row, status="rejected", reason="but market order did not fill in 6s", minutes_ago=m))
        # An hour of the rail talking to itself, newer than the real evidence.
        for m in range(60, 0, -1):
            s.add(_row(strategy_row, status="rejected",
                       reason="but rail: cooling off after 3 non-fills (0.5h of 1h)", minutes_ago=m))

    strikes, last = _strikes(strategy_row)
    assert strikes == 3, "the rail's own rows evicted the non-fills that justified the cooldown"
    # And the clock still runs from the last real miss, not from the noise.
    assert abs((last - (NOW - timedelta(minutes=198))).total_seconds()) < 2
