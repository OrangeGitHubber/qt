"""Table-driven tests for the pure reconcile() function, plus one impure
apply_reconciliation() round-trip against a mocked Alpaca."""

from unittest.mock import AsyncMock, patch

import pytest

from qt import security
from qt.broker.alpaca import SECRET_KEY_ID, SECRET_KEY_SECRET, AlpacaClient
from qt.db import session_scope
from qt.models import Strategy, Trade
from qt.services import reconcile as reconcile_mod
from qt.services.reconcile import (
    Action,
    OpenTradeView,
    OrderView,
    PositionView,
    reconcile,
)
from qt.settings_service import set_setting


def _trade(**kw) -> OpenTradeView:
    base = dict(id=1, symbol="AAA", qty=10, entry_order_id="o1", entry_confirmed=True, last_price=100.0)
    base.update(kw)
    return OpenTradeView(**base)


def _kinds(actions: list[Action]) -> list[str]:
    return [a.kind for a in actions]


def test_in_sync_confirmed_trade_with_position_no_action():
    actions = reconcile(
        [_trade()],
        [PositionView("AAA", qty=10, current_price=101.0)],
        [],
    )
    assert actions == []


def test_confirmed_trade_missing_position_is_closed():
    # Case (a): we believed we held it; broker doesn't -> exit filled while down.
    actions = reconcile([_trade(last_price=95.0)], [], [])
    assert _kinds(actions) == ["close_reconciled"]
    assert actions[0].price == 95.0
    assert actions[0].trade_id == 1


def test_crypto_slash_symbol_matches_broker_slashless():
    # We store 'AVAX/USD'; Alpaca /v2/positions returns 'AVAXUSD'. These must
    # reconcile as the SAME position — otherwise every crypto trade is wrongly
    # closed on the next cycle.
    actions = reconcile(
        [_trade(symbol="AVAX/USD", qty=147.8)],
        [PositionView("AVAXUSD", qty=147.8, current_price=6.8)],
        [],
    )
    assert actions == []  # in sync, no wrongful close


def test_orphan_position_alerts_never_adopts():
    # Case (b): broker holds ZZZ, DB knows nothing about it.
    actions = reconcile([], [PositionView("ZZZ", qty=3, current_price=50.0)], [])
    assert _kinds(actions) == ["alert_orphan_position"]
    assert actions[0].symbol == "ZZZ"
    assert actions[0].qty == 3


def test_zero_qty_position_ignored():
    # A flat position (qty 0) is not a real holding.
    actions = reconcile([], [PositionView("ZZZ", qty=0.0)], [])
    assert actions == []


def test_unconfirmed_entry_that_filled_is_confirmed():
    # Case (c): recorded but never confirmed; the entry order shows filled.
    t = _trade(entry_confirmed=False, entry_order_id="o9", qty=0)
    order = OrderView(id="o9", symbol="AAA", status="filled", filled_qty=7, filled_avg_price=102.5)
    actions = reconcile([t], [PositionView("AAA", qty=7, current_price=103.0)], [order])
    assert _kinds(actions) == ["confirm_entry"]
    assert actions[0].qty == 7
    assert actions[0].price == 102.5


def test_unconfirmed_entry_with_position_but_no_order_confirms_from_position():
    t = _trade(entry_confirmed=False, entry_order_id=None)
    actions = reconcile([t], [PositionView("AAA", qty=10, current_price=99.0)], [])
    assert _kinds(actions) == ["confirm_entry"]
    assert actions[0].price == 99.0


def test_unconfirmed_entry_still_working_is_awaited():
    t = _trade(entry_confirmed=False, entry_order_id="o9")
    order = OrderView(id="o9", symbol="AAA", status="new")
    actions = reconcile([t], [], [order])
    assert _kinds(actions) == ["await_entry"]


def test_unconfirmed_entry_dead_order_no_position_is_rejected():
    t = _trade(entry_confirmed=False, entry_order_id="o9")
    order = OrderView(id="o9", symbol="AAA", status="canceled")
    actions = reconcile([t], [], [order])
    assert _kinds(actions) == ["reject_entry"]


def test_unconfirmed_entry_no_order_no_position_is_rejected():
    t = _trade(entry_confirmed=False, entry_order_id=None)
    actions = reconcile([t], [], [])
    assert _kinds(actions) == ["reject_entry"]


def test_mixed_scenario():
    trades = [
        _trade(id=1, symbol="AAA"),  # confirmed, has position -> no action
        _trade(id=2, symbol="BBB", last_price=20.0),  # confirmed, no position -> close
    ]
    positions = [
        PositionView("AAA", qty=10, current_price=100.0),
        PositionView("CCC", qty=5, current_price=8.0),  # orphan
    ]
    actions = reconcile(trades, positions, [])
    kinds = {(a.kind, a.symbol) for a in actions}
    assert ("close_reconciled", "BBB") in kinds
    assert ("alert_orphan_position", "CCC") in kinds
    assert not any(a.symbol == "AAA" for a in actions)


# ---- impure shell round-trip ----


@pytest.fixture()
def paper_trade_world(client):
    with session_scope() as s:
        security.set_secret(s, SECRET_KEY_ID, "k")
        security.set_secret(s, SECRET_KEY_SECRET, "s")
        set_setting(s, "engine_mode", "paper")
        strat = Strategy(
            name="recon", enabled=True, asset_class="stock", universe="scanner",
            preset="custom", params="{}", sizing_usd=200, sleeve_usd=1000, max_positions=3,
        )
        s.add(strat)
        s.flush()
        s.add(Trade(
            strategy_id=strat.id, mode="paper", symbol="AAA", asset_class="stock",
            qty=10, notional=1000, status="open", entry_price=100.0,
            entry_order_id="o1", high_water=105.0,
        ))
        sid = strat.id
    yield sid
    with session_scope() as s:
        set_setting(s, "engine_mode", "off")
        s.query(Trade).delete()
        s.query(Strategy).filter(Strategy.id == sid).delete()
        security.delete_secret(s, SECRET_KEY_ID)
        security.delete_secret(s, SECRET_KEY_SECRET)


async def test_apply_reconciliation_closes_missing_position(paper_trade_world):
    """Books at the BROKER'S FILL, not at the high-water mark.

    This used to assert 105.0 — the peak the position reached while held. That
    is >= the entry by construction, so `(price - entry) * qty` could never come
    out negative, and the live journal duly showed 105 reconciled closes worth
    +$101.36 with not one loss among them. The fill is the answer to a question
    QT cannot answer itself, so it asks."""
    fills = [
        {"symbol": "AAA", "side": "sell", "price": "96.5",
         "transaction_time": "2099-01-01T00:00:00Z"},
    ]
    with patch.multiple(
        AlpacaClient,
        list_positions=AsyncMock(return_value=[]),  # broker holds nothing
        list_orders=AsyncMock(return_value=[]),
        account_activities=AsyncMock(return_value=fills),
    ):
        with session_scope() as s:
            from qt.broker.factory import get_client

            actions = await reconcile_mod.apply_reconciliation(s, get_client(s), "paper")
    assert [a.kind for a in actions] == ["close_reconciled"]
    with session_scope() as s:
        trade = s.query(Trade).filter(Trade.symbol == "AAA").one()
        assert trade.status == "closed"
        assert trade.exit_price == 96.5, "the broker's fill, not the 105.0 high-water"
        assert trade.pnl < 0, "a position sold below entry must book a LOSS"
        assert "reconciled" in trade.exit_reason


async def test_a_close_with_no_findable_fill_books_zero(paper_trade_world):
    """The fallback. When the broker shows no matching sell — it aged out of the
    window, or the call failed — the exit price is genuinely unknown. Booking the
    entry price makes the P&L read zero, which is also wrong but does not LEAN:
    an unknown exit must not flatter the strategy that owned it."""
    with patch.multiple(
        AlpacaClient,
        list_positions=AsyncMock(return_value=[]),
        list_orders=AsyncMock(return_value=[]),
        account_activities=AsyncMock(return_value=[]),
    ):
        with session_scope() as s:
            from qt.broker.factory import get_client

            await reconcile_mod.apply_reconciliation(s, get_client(s), "paper")
    with session_scope() as s:
        trade = s.query(Trade).filter(Trade.symbol == "AAA").one()
        assert trade.exit_price == 100.0, "entry price, NOT the 105.0 high-water"
        assert trade.pnl == 0.0
        assert "no matching broker fill" in trade.exit_reason


async def test_a_broker_error_reading_fills_does_not_block_the_close(paper_trade_world):
    """Reconciliation is the RECOVERY path. If it dies on a failed activities
    call, the journal is left in exactly the state it was called to repair — so
    the fill lookup degrades to the zero-P&L fallback rather than raising."""
    with patch.multiple(
        AlpacaClient,
        list_positions=AsyncMock(return_value=[]),
        list_orders=AsyncMock(return_value=[]),
        account_activities=AsyncMock(side_effect=RuntimeError("boom")),
    ):
        with session_scope() as s:
            from qt.broker.factory import get_client

            actions = await reconcile_mod.apply_reconciliation(s, get_client(s), "paper")
    assert [a.kind for a in actions] == ["close_reconciled"]
    with session_scope() as s:
        trade = s.query(Trade).filter(Trade.symbol == "AAA").one()
        assert trade.status == "closed"
        assert trade.pnl == 0.0
