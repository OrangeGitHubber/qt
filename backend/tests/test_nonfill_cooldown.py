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
