"""Rails: what they are scoped to, and what they write while they hold.

Two of these come from measured behaviour in the live container: 500 journal
rows in 54 minutes from one crypto probe, 497 of them rejections, 353 of those
one rail repeating itself — against a symbol that strategy had never once
filled.
"""

from datetime import datetime, timedelta, timezone

import pytest

from qt.db import session_scope
from qt.models import Strategy, Trade
from qt.services.engine import (
    RISK_DEFAULTS,
    _build_rail_context,
    _rail_rejection_is_new,
    _rail_signature,
    check_rails,
)

NOW = datetime.now(timezone.utc)


def _strategy(session, name: str, asset_class: str = "crypto") -> int:
    strat = Strategy(
        name=name, enabled=True, asset_class=asset_class, universe="scanner",
        preset="custom", params='{"entry":{},"exit":{"stop_loss_pct":4}}',
        sizing_usd=100, sleeve_usd=1000, max_positions=3, swing_mode=False,
        ignore_regime=False,
    )
    session.add(strat)
    session.flush()
    return strat.id


@pytest.fixture()
def two_strategies():
    with session_scope() as s:
        loser = _strategy(s, "Momentum sleeve")
        blocked = _strategy(s, "Fidelity probe")
    yield loser, blocked
    with session_scope() as s:
        s.query(Trade).filter(Trade.strategy_id.in_((loser, blocked))).delete(synchronize_session=False)
        s.query(Strategy).filter(Strategy.id.in_((loser, blocked))).delete(synchronize_session=False)


def _ctx(session, sid: int, symbol: str, mode: str = "paper"):
    return _build_rail_context(
        session, mode, session.get(Strategy, sid), symbol, 10_000.0,
        dict(RISK_DEFAULTS), False, 0.0, NOW - timedelta(days=1),
    )


# --------------------------------------------------------------------------
# The post-loss cooldown is ACCOUNT-WIDE by design. The message wasn't.
# --------------------------------------------------------------------------


def test_the_cooldown_rejection_names_the_strategy_that_actually_lost(two_strategies):
    """The scope is deliberate — RailContext documents it, _build_rail_context
    documents it, and the Strategies UI states it. The MESSAGE did not, so a
    strategy blocked out of a symbol it had never traded read as broken: 353
    rows saying "cooldown after loss" about somebody else's loss."""
    loser, blocked = two_strategies
    with session_scope() as s:
        s.add(Trade(
            strategy_id=loser, mode="paper", symbol="AAVE/USD", asset_class="crypto",
            qty=1, notional=100, status="closed", entry_price=100.0, exit_price=90.0,
            pnl=-10.0, entry_at=NOW - timedelta(hours=4), exit_at=NOW - timedelta(hours=2),
        ))

    with session_scope() as s:
        ctx = _ctx(s, blocked, "AAVE/USD")

    assert ctx.last_loss_at is not None, "the cooldown is account-wide — it must still fire"
    ok, reason = check_rails(
        {"max_positions": 3, "sleeve_usd": 1000, "allow_concurrent_symbol": False}, 100.0, ctx
    )
    assert ok is False
    # Claim one: it says whose loss it was.
    assert "Momentum sleeve" in reason, (
        "the row blamed a strategy that never filled this symbol for its own loss"
    )
    # Claim two: it says the rail is not per-strategy.
    assert "account-wide" in reason


def test_the_cooldown_says_this_strategy_when_it_really_was(two_strategies):
    """The other half of the same claim — naming must not become noise on the
    ordinary case where the blocked strategy is the one that lost."""
    loser, _blocked = two_strategies
    with session_scope() as s:
        s.add(Trade(
            strategy_id=loser, mode="paper", symbol="SOL/USD", asset_class="crypto",
            qty=1, notional=100, status="closed", entry_price=100.0, exit_price=90.0,
            pnl=-10.0, entry_at=NOW - timedelta(hours=4), exit_at=NOW - timedelta(hours=2),
        ))
    with session_scope() as s:
        ctx = _ctx(s, loser, "SOL/USD")
    _ok, reason = check_rails(
        {"max_positions": 3, "sleeve_usd": 1000, "allow_concurrent_symbol": False}, 100.0, ctx
    )
    assert "this strategy" in reason
    assert "Momentum sleeve" not in reason


# --------------------------------------------------------------------------
# The wash-sale guard was the one query here with no mode filter.
# --------------------------------------------------------------------------


@pytest.fixture()
def stock_strategy():
    with session_scope() as s:
        sid = _strategy(s, "wash audit", asset_class="stock")
    yield sid
    with session_scope() as s:
        s.query(Trade).filter(Trade.strategy_id == sid).delete()
        s.query(Strategy).filter(Strategy.id == sid).delete()


def test_a_shadow_mode_loss_cannot_wash_sale_block_a_paper_entry(stock_strategy):
    """Shadow mode places no orders, so a shadow "loss" is a sale that never
    happened and that the IRS has never heard of. Every other query in
    _build_rail_context filters by mode; this one did not, so weeks of shadow
    journal would block real entries for 31 days each."""
    with session_scope() as s:
        s.add(Trade(
            strategy_id=stock_strategy, mode="shadow", symbol="AAPL", asset_class="stock",
            qty=1, notional=100, status="closed", entry_price=100.0, exit_price=90.0,
            pnl=-10.0, entry_at=NOW - timedelta(days=3), exit_at=NOW - timedelta(days=2),
        ))

    with session_scope() as s:
        ctx = _ctx(s, stock_strategy, "AAPL", mode="paper")
    assert ctx.loss_sale_within_31d is False, (
        "a simulated shadow-mode sale blocked a real paper entry for 31 days"
    )

    # The guard itself still works — this is a scoping fix, not a removal.
    with session_scope() as s:
        ctx_shadow = _ctx(s, stock_strategy, "AAPL", mode="shadow")
    assert ctx_shadow.loss_sale_within_31d is True


# --------------------------------------------------------------------------
# A rail that blocks on every tick used to journal on every tick.
# --------------------------------------------------------------------------


def test_the_rail_signature_ignores_the_numbers_that_change_every_tick():
    a = _rail_signature("wanted to buy (up 1.31% today) but rail: cooldown after loss (2.9h of 24.0h)")
    b = _rail_signature("wanted to buy (up 1.40% today) but rail: cooldown after loss (3.0h of 24.0h)")
    assert a == b == "cooldown after loss"
    # A DIFFERENT rail is a different row — suppression must not swallow news.
    assert _rail_signature("but rail: sleeve budget exceeded ($900 held + $100 > $1,000)") != a
    # Nothing that names no rail may ever match anything (execution's own rows).
    assert _rail_signature("wanted to buy (x) but market order did not fill in 6s") == ""


def test_a_rail_blocking_every_tick_is_journalled_once_not_once_per_tick(two_strategies):
    """A 24-hour cooldown blocks on all 1,440 ticks of the day. Journalling each
    one buried the answer to "why didn't it trade?" under the answer, repeated —
    the same shape as the non-fill breaker's own rows evicting its evidence."""
    _loser, sid = two_strategies
    reason = "rail: cooldown after loss (2.9h of 24.0h) — account-wide on this symbol"

    with session_scope() as s:
        assert _rail_rejection_is_new(s, "paper", sid, "AAVE/USD", reason) is True
        s.add(Trade(
            strategy_id=sid, mode="paper", symbol="AAVE/USD", asset_class="crypto",
            qty=0, notional=0, status="rejected",
            entry_reason=f"wanted to buy (up 1.31% today) but {reason}",
            created_at=NOW - timedelta(minutes=1),
        ))
        s.flush()

        # A minute later, same rail, different numbers: not news.
        later = "rail: cooldown after loss (3.0h of 24.0h) — account-wide on this symbol"
        assert _rail_rejection_is_new(s, "paper", sid, "AAVE/USD", later) is False

        # A DIFFERENT rail is news even one second later.
        assert _rail_rejection_is_new(
            s, "paper", sid, "AAVE/USD", "rail: sleeve budget exceeded ($900 + $100 > $1,000)"
        ) is True

        # And another symbol is never suppressed by this one's row.
        assert _rail_rejection_is_new(s, "paper", sid, "SOL/USD", later) is True


def test_a_long_block_is_refreshed_so_it_never_vanishes_from_a_recent_view(two_strategies):
    """Suppression must not become silence: a rail that has held for hours still
    has to appear in a "last hour" view of the journal."""
    _loser, sid = two_strategies
    reason = "rail: cooldown after loss (9.0h of 24.0h) — account-wide on this symbol"
    with session_scope() as s:
        s.add(Trade(
            strategy_id=sid, mode="paper", symbol="AAVE/USD", asset_class="crypto",
            qty=0, notional=0, status="rejected",
            entry_reason=f"wanted to buy (up 1.31% today) but {reason}",
            created_at=NOW - timedelta(minutes=90),
        ))
        s.flush()
        assert _rail_rejection_is_new(s, "paper", sid, "AAVE/USD", reason) is True


def test_a_reason_that_names_no_rail_is_never_suppressed(two_strategies):
    """Suppression keys on the RAIL in the reason, and rows that name no rail —
    execution's own "did not fill" rows, which the non-fill breaker counts as
    strikes — all share the empty signature. Without the guard for that, two
    consecutive non-fills would look like the same block and the second would be
    dropped, costing the breaker a strike by a brand-new route."""
    _loser, sid = two_strategies
    non_fill = "wanted to buy (up 2%) but market order did not fill in 6s"
    with session_scope() as s:
        s.add(Trade(
            strategy_id=sid, mode="paper", symbol="AAVE/USD", asset_class="crypto",
            qty=0, notional=0, status="rejected", entry_reason=non_fill,
            created_at=NOW - timedelta(seconds=30),
        ))
        s.flush()
        assert _rail_rejection_is_new(s, "paper", sid, "AAVE/USD", non_fill) is True
        # And a rail arriving right after a non-fill is news in its own right.
        assert _rail_rejection_is_new(
            s, "paper", sid, "AAVE/USD", "rail: cooldown after loss (0.1h of 24.0h)"
        ) is True
