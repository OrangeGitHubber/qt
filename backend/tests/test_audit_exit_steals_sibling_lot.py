"""One strategy's exit could liquidate another strategy's position.

The broker reports ONE net position per symbol. Once two strategies may hold the
same coin (Strategy.allow_concurrent_symbol), `_broker_held_qty` returns both
their lots added together — and the exit path clamped to it:

    held = await _broker_held_qty(client, trade.symbol)
    if held is not None and held < trade.qty:
        sell_qty = held                      # <-- everyone's coins

So whenever this trade's journal read higher than the broker's total, the exit
sold the WHOLE symbol, including lots belonging to strategies that had not asked
to exit anything. Their trade rows then stayed open against nothing, for ever.

FOUND IN THE LIVE JOURNAL, AVAX/USD, three strategies (18, 22, 27) holding it at
once:

    2026-08-06 09:14  SELL 14.7277 AVAX/USD  (strategy 18, trailing stop)
    2026-08-06 09:21  SELL 14.8231 AVAX/USD  (strategy 22, trailing stop)
    2026-08-06 10:04  SELL NOT SUBMITTED — 0.139527 left is worth $0.90
    2026-08-06 14:13  SELL FAILED — insufficient balance for AVAX
                      (requested: 15.055479, available: 0.13952706)

Strategy 27's trade 84076 has been open against a position that does not exist
from 2026-08-07 to the day this was written, and was the source of a quantity
mismatch alert every fifteen minutes.

THE RULE IS PRO RATA, and the alternatives were both worse:
  * FIRST COME lets one strategy take another's lot in full — the bug itself.
  * REFUSING TO SELL when the total is short is "safe" for the accounting and
    strands a falling position with a live stop-loss against it, which is the
    one outcome an exit exists to prevent.
Pro rata is also the rule reconcile already applies to the same shape of
problem, scaling each trade in the group "in proportion to its own size".

The P&L side needed no change: the close already books on `filled_qty` rather
than on what was asked for.
"""

import pytest

from qt.models import Strategy, Trade
from qt.services.execution import _sibling_open_qty, attributable_qty

# Alpaca's crypto commission, taken in the coin — the routine shortfall this
# path exists to absorb, and which must keep being absorbed.
FEE = 0.0025


# ── the arithmetic ───────────────────────────────────────────────────────────
def test_a_lone_trade_behaves_exactly_as_before():
    """No siblings means no sharing, and this is the overwhelmingly common case.
    Changing it would be a far larger regression than the bug being fixed."""
    assert attributable_qty(10.0, 0.0, 100.0) == 10.0     # plenty held
    assert attributable_qty(10.0, 0.0, 9.0) == 9.0        # clamped to held
    assert attributable_qty(10.0, 0.0, 0.0) == 0.0


def test_the_fee_in_kind_haircut_is_still_absorbed():
    """The reason the clamp exists at all: the journal reads ~0.25% above the
    broker for up to 15 minutes after any crypto entry."""
    mine = 10.0
    got = attributable_qty(mine, 0.0, mine * (1 - FEE))
    assert abs(got - 9.975) < 1e-9
    assert got < mine


def test_a_sibling_lot_is_never_taken():
    """THE BUG. Two strategies hold 15 each; the broker has both lots but is a
    little short. The old code sold `held` — 29 of a 15-coin claim, i.e. all of
    this trade's coins plus 14 of its neighbour's."""
    mine, sibling, held = 15.0, 15.0, 29.0
    got = attributable_qty(mine, sibling, held)
    assert got < mine, "a shortfall must still reduce the order"
    assert got <= held - sibling + 1e-9 or got < held, got
    # Pro rata: each of two equal claims gets half of what is actually there.
    assert abs(got - 14.5) < 1e-9, got
    # And the neighbour, asked the same question, gets the other half — the two
    # together must never exceed what the broker holds.
    other = attributable_qty(sibling, mine, held)
    assert abs(got + other - held) < 1e-9, (got, other)


def test_werners_avax_case():
    """Three strategies, and by the time the third asked there was nothing left.
    Its share of 0.1395 across three equal claims is ~0.0465 — which the caller
    then refuses outright for being under Alpaca's $1 minimum. What must NOT
    happen is the old answer: sell all 0.1395, two thirds of it not ours."""
    got = attributable_qty(15.055479, 15.0 + 15.0, 0.13952706)
    assert got < 0.13952706, "the whole remaining position was taken before"
    assert abs(got - 0.13952706 * (15.055479 / 45.055479)) < 1e-9


def test_shares_are_proportional_to_claim_size_not_equal():
    """A trade claiming three times as much gets three times the share. Splitting
    a shortfall evenly would penalise the larger position."""
    big = attributable_qty(30.0, 10.0, 20.0)
    small = attributable_qty(10.0, 30.0, 20.0)
    assert abs(big - 15.0) < 1e-9
    assert abs(small - 5.0) < 1e-9
    assert abs(big + small - 20.0) < 1e-9


def test_a_surplus_at_the_broker_is_not_shared_away():
    """When the broker holds MORE than the open trades claim between them, each
    trade sells its own full quantity. Pro-rating here would under-sell every
    exit and leave residue behind on purpose."""
    assert attributable_qty(10.0, 10.0, 25.0) == 10.0
    assert attributable_qty(10.0, 10.0, 20.0) == 10.0


def test_nothing_held_sells_nothing():
    assert attributable_qty(10.0, 5.0, 0.0) == 0.0


def test_a_negative_broker_quantity_sells_nothing():
    """Alpaca reports a SHORT position as a negative quantity. QT is long-only,
    so this should not arise — but the result is fed straight into an order, and
    a negative sell quantity is not something to find out about at the broker.

    This is here because it had to be: the `max(0.0, ...)` survived its mutation
    while an unreachable branch stood in front of it."""
    assert attributable_qty(10.0, 0.0, -5.0) == 0.0
    assert attributable_qty(10.0, 5.0, -0.001) == 0.0


def test_a_zero_claim_cannot_divide_by_zero():
    """Reachable: a part-filled exit shrinks trade.qty, and a sibling row can sit
    at 0 between an adjustment and its commit."""
    assert attributable_qty(0.0, 0.0, 5.0) == 0.0
    assert attributable_qty(0.0, 10.0, 5.0) == 0.0


def test_the_result_is_never_negative_and_never_exceeds_the_claim():
    """The two invariants the caller depends on: it passes the result to
    `min(sell_qty, mine)` and then to an order."""
    for mine, sib, held in [(10, 5, 3), (1, 100, 2), (5, 0, 999), (0.001, 0.002, 0.0005)]:
        got = attributable_qty(mine, sib, held)
        assert 0.0 <= got <= mine + 1e-12, (mine, sib, held, got)


# ── who counts as a sibling ──────────────────────────────────────────────────
SYMBOLS = ["AVAX/USD", "SOL/USD"]


@pytest.fixture()
def clean_trades(db_session):
    """A real strategy row, because Trade.strategy_id is a NOT NULL foreign key,
    and a clean slate for these two symbols — the shared test database is not
    rolled back between tests and a leftover open row would be counted as a
    sibling by the very function under test."""
    strat = Strategy(
        name="sibling audit", enabled=False, asset_class="crypto", universe="scanner",
        preset="custom", params='{"entry":{},"exit":{"stop_loss_pct":4}}',
        sizing_usd=100, sleeve_usd=500, max_positions=3, swing_mode=False,
        ignore_regime=False,
    )
    db_session.add(strat)
    db_session.flush()

    def clear():
        db_session.query(Trade).filter(Trade.symbol.in_(SYMBOLS)).delete(
            synchronize_session=False)
        db_session.flush()

    clear()
    db_session.strategy_id = strat.id
    yield db_session
    clear()
    db_session.query(Strategy).filter(Strategy.id == strat.id).delete()
    db_session.flush()


def _trade(session, symbol="AVAX/USD", qty=15.0, status="open", mode="paper"):
    t = Trade(
        strategy_id=session.strategy_id, mode=mode, symbol=symbol,
        asset_class="crypto", qty=qty, notional=qty * 6.5, status=status,
        entry_price=6.5, entry_order_id="o-x",
    )
    session.add(t)
    session.flush()
    return t


def test_a_trade_is_not_its_own_sibling(clean_trades):
    """The whole calculation collapses if it is: `mine + siblings` would double
    count and every exit would halve itself."""
    t = _trade(clean_trades)
    assert _sibling_open_qty(clean_trades, t) == 0.0


def test_another_open_trade_on_the_symbol_counts(clean_trades):
    t = _trade(clean_trades, qty=15.0)
    _trade(clean_trades, qty=7.0)
    assert _sibling_open_qty(clean_trades, t) == 7.0


def test_closed_and_rejected_trades_do_not_count(clean_trades):
    """They hold no coins. Counting them would shrink every live exit — and the
    live journal has 2,864 rejected AVAX rows against one open trade, so this is
    not a hypothetical margin."""
    t = _trade(clean_trades, qty=15.0)
    _trade(clean_trades, qty=99.0, status="closed")
    _trade(clean_trades, qty=99.0, status="rejected")
    assert _sibling_open_qty(clean_trades, t) == 0.0


def test_a_different_symbol_does_not_count(clean_trades):
    t = _trade(clean_trades, symbol="AVAX/USD", qty=15.0)
    _trade(clean_trades, symbol="SOL/USD", qty=99.0)
    assert _sibling_open_qty(clean_trades, t) == 0.0


def test_the_other_mode_does_not_count(clean_trades):
    """Paper and live are separate accounts with separate positions. Letting a
    live trade shrink a paper exit would be a cross-account leak."""
    t = _trade(clean_trades, qty=15.0, mode="paper")
    _trade(clean_trades, qty=99.0, mode="live")
    assert _sibling_open_qty(clean_trades, t) == 0.0
