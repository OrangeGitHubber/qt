"""Two strategies holding the same symbol.

The account-wide "one position per symbol" rail is the default and stays it.
This covers the opt-out: what it allows, what it must NOT allow, and the
reconciliation that had to become quantity-aware before it was safe to offer —
the broker reports one net position per symbol, so a symbol match stopped being
an identity match.
"""

from datetime import datetime, timezone

from qt.services.engine import RailContext, check_rails
from qt.services.reconcile import OpenTradeView, OrderView, PositionView, reconcile

RISK = {"max_daily_loss_usd": 1e9, "max_daily_loss_pct": 100, "max_total_positions": 50,
        "max_total_exposure_usd": 1e9, "max_trades_per_day": 200,
        "cooldown_hours_after_loss": 24, "wash_sale_guard": "block", "leverage_enabled": False}


def _ctx(**over) -> RailContext:
    base = dict(
        equity=100_000.0, open_positions_total=1, open_exposure_usd=1000.0,
        open_positions_strategy=0, open_exposure_strategy_usd=0.0, entries_today=1,
        already_open_symbol=False, last_loss_at=None, loss_sale_within_31d=False, risk=dict(RISK),
    )
    base.update(over)
    return RailContext(**base)


def _cfg(**over) -> dict:
    base = {"max_positions": 5, "sleeve_usd": 10_000, "allow_concurrent_symbol": False}
    base.update(over)
    return base


# --- the rail -------------------------------------------------------------

def test_the_default_still_blocks_a_symbol_another_strategy_holds():
    ok, why = check_rails(_cfg(), 1000, _ctx(already_open_symbol=True))
    assert not ok and "any strategy" in why


def test_the_opt_in_names_the_narrower_rule_when_it_blocks():
    """With the flag on, already_open_symbol means 'THIS strategy holds it' —
    the reason has to say so or it reads like the old account-wide block."""
    ok, why = check_rails(_cfg(allow_concurrent_symbol=True), 1000, _ctx(already_open_symbol=True))
    assert not ok and "this strategy already holds" in why


def test_the_wash_sale_guard_is_not_relaxed_by_the_opt_in():
    """Explicit: wash sales are counted against the ACCOUNT, so sharing a symbol
    between strategies must not become a way around the guard."""
    ok, why = check_rails(
        _cfg(allow_concurrent_symbol=True), 1000, _ctx(loss_sale_within_31d=True)
    )
    assert not ok and "wash" in why.lower()


def test_the_loss_cooldown_is_not_relaxed_by_the_opt_in():
    recent = datetime.now(timezone.utc)
    ok, why = check_rails(
        _cfg(allow_concurrent_symbol=True), 1000, _ctx(last_loss_at=recent)
    )
    assert not ok and "cooldown" in why.lower()


# --- reconciliation -------------------------------------------------------

def _t(tid, qty, confirmed=True, order=None):
    return OpenTradeView(id=tid, symbol="NVDA", qty=qty, entry_order_id=order,
                         entry_confirmed=confirmed, last_price=100.0)


def test_an_unconfirmed_entry_does_not_swallow_the_other_strategys_shares():
    """THE bug this had to fix first. A holds 5, B's entry is unconfirmed, the
    broker shows 8. Adopting pos.qty would give B all 8 — the journal would then
    claim 13 against a real 8."""
    actions = reconcile(
        [_t(1, 5.0), _t(2, 3.0, confirmed=False)],
        [PositionView("NVDA", 8.0, current_price=110.0)],
        [],
    )
    confirm = [a for a in actions if a.kind == "confirm_entry"]
    assert len(confirm) == 1
    assert confirm[0].qty == 3.0, "adopted the whole broker position into one strategy's trade"


def test_a_lone_trade_still_adopts_the_brokers_quantity():
    """The counterpart: with one claimant the broker IS the truth, and that
    behaviour must not have been lost."""
    actions = reconcile(
        [_t(1, 5.0, confirmed=False)],
        [PositionView("NVDA", 7.0, current_price=110.0)],
        [],
    )
    confirm = [a for a in actions if a.kind == "confirm_entry"]
    assert len(confirm) == 1 and confirm[0].qty == 7.0


def test_totals_that_disagree_with_the_broker_are_reported():
    """Two strategies claiming 8 while the broker holds 6 is drift. Report it —
    'a position exists' used to be the whole check."""
    actions = reconcile(
        [_t(1, 5.0), _t(2, 3.0)],
        [PositionView("NVDA", 6.0, current_price=110.0)],
        [],
    )
    mismatch = [a for a in actions if a.kind == "alert_qty_mismatch"]
    assert len(mismatch) == 1
    assert "2 strategies" in mismatch[0].reason


def test_matching_totals_are_silent():
    actions = reconcile(
        [_t(1, 5.0), _t(2, 3.0)],
        [PositionView("NVDA", 8.0, current_price=110.0)],
        [],
    )
    assert [a for a in actions if a.kind == "alert_qty_mismatch"] == []


def test_a_vanished_position_still_closes_every_trade_on_it():
    """Unchanged and still right for many holders: if the broker holds none of a
    symbol, nobody holds any of it."""
    actions = reconcile([_t(1, 5.0), _t(2, 3.0)], [], [])
    closed = [a for a in actions if a.kind == "close_reconciled"]
    assert {a.trade_id for a in closed} == {1, 2}


def test_an_unconfirmed_entry_whose_order_filled_uses_the_orders_own_fill():
    """When the order itself tells us the fill, that beats any inference from
    the shared position."""
    actions = reconcile(
        [_t(1, 5.0), _t(2, 0.0, confirmed=False, order="o2")],
        [PositionView("NVDA", 8.0, current_price=110.0)],
        [OrderView(id="o2", symbol="NVDA", status="filled", filled_qty=3.0, filled_avg_price=101.0)],
    )
    confirm = [a for a in actions if a.kind == "confirm_entry"]
    assert len(confirm) == 1 and confirm[0].qty == 3.0
