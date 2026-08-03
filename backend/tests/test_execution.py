from unittest.mock import AsyncMock, patch

import pytest

from qt.broker.alpaca import AlpacaClient
from qt.db import session_scope
from qt.models import Strategy, Trade
from qt.services import execution
from qt.services.engine import Candidate


@pytest.fixture()
def strategy_row():
    with session_scope() as s:
        strat = Strategy(
            name="exec test", enabled=True, asset_class="stock", universe="scanner",
            preset="custom", params='{"entry":{},"exit":{"stop_loss_pct":4}}',
            sizing_usd=200, sleeve_usd=1000, max_positions=3, swing_mode=True, ignore_regime=False,
        )
        s.add(strat)
        s.flush()
        sid = strat.id
    yield sid
    with session_scope() as s:
        s.query(Trade).filter(Trade.strategy_id == sid).delete()
        s.query(Strategy).filter(Strategy.id == sid).delete()


def _client() -> AlpacaClient:
    return AlpacaClient(key_id="k", key_secret="s")


async def test_paper_buy_uses_marketable_limit_and_fill_price(strategy_row):
    submitted = {}

    async def fake_submit(self, symbol, qty, side, limit_price, client_order_id, time_in_force="day"):
        submitted.update(symbol=symbol, qty=qty, side=side, limit=limit_price, tif=time_in_force)
        return {"id": "order-1", "status": "accepted"}

    filled = {"id": "order-1", "status": "filled", "filled_avg_price": "21.18", "filled_qty": "9"}
    with (
        patch.object(AlpacaClient, "submit_order", fake_submit),
        patch.object(AlpacaClient, "get_order", new=AsyncMock(return_value=filled)),
        patch("qt.services.execution.FILL_POLL_SECONDS", (0,)),
    ):
        with session_scope() as s:
            strat = s.get(Strategy, strategy_row)
            cand = Candidate(symbol="ROCKET", asset_class="stock", price=21.2, change_pct=6.0, vwap=20.8)
            trade = await execution.open_trade(s, _client(), strat, None, "paper", cand, "test entry")
            assert trade is not None
            assert trade.entry_price == 21.18  # actual fill price, not quote
            assert trade.entry_order_id == "order-1"

    assert submitted["side"] == "buy"
    assert submitted["qty"] == 9  # whole shares only for stocks
    assert submitted["limit"] == round(21.2 * 1.005, 2)  # marketable limit, never market


async def test_paper_unfilled_buy_is_cancelled_and_journaled(strategy_row):
    pending = {"id": "order-2", "status": "new"}
    cancels = []

    async def fake_cancel(self, order_id):
        cancels.append(order_id)

    with (
        patch.object(AlpacaClient, "submit_order", new=AsyncMock(return_value={"id": "order-2"})),
        patch.object(AlpacaClient, "get_order", new=AsyncMock(return_value=pending)),
        patch.object(AlpacaClient, "cancel_order", fake_cancel),
        patch("qt.services.execution.FILL_POLL_SECONDS", (0,)),
    ):
        with session_scope() as s:
            strat = s.get(Strategy, strategy_row)
            cand = Candidate(symbol="ROCKET", asset_class="stock", price=21.2, change_pct=6.0, vwap=20.8)
            trade = await execution.open_trade(s, _client(), strat, None, "paper", cand, "test entry")
            assert trade is None

        with session_scope() as s:
            rejected = s.query(Trade).filter(Trade.strategy_id == strategy_row, Trade.status == "rejected").all()
            assert len(rejected) == 1
            assert "did not fill" in rejected[0].entry_reason
    assert cancels == ["order-2"]


async def test_unfilled_buy_reports_the_brokers_status_not_our_own_cancel(strategy_row):
    """RENDER/USD retried every 60s for 41 minutes and every journal row said only
    "did not fill" — which covers still-working, expired, and rejected-after-
    acceptance alike, so the row could not say which. The row must carry the
    status the order had when we gave up, NOT the "canceled" our own cancel
    produces a moment later."""
    # Polled twice as still-working, then cancelled by us — the re-read afterwards
    # reports our cancel. Reporting that would be reporting our own action back.
    statuses = [
        {"id": "order-9", "status": "pending_new", "filled_qty": "0"},
        {"id": "order-9", "status": "pending_new", "filled_qty": "0"},
        {"id": "order-9", "status": "canceled", "filled_qty": "0"},
    ]

    with (
        patch.object(AlpacaClient, "submit_order", new=AsyncMock(return_value={"id": "order-9"})),
        patch.object(AlpacaClient, "get_order", new=AsyncMock(side_effect=statuses)),
        patch.object(AlpacaClient, "cancel_order", new=AsyncMock(return_value=None)),
        patch("qt.services.execution.FILL_POLL_SECONDS", (0, 0)),
    ):
        with session_scope() as s:
            strat = s.get(Strategy, strategy_row)
            cand = Candidate(symbol="ROCKET", asset_class="stock", price=21.2, change_pct=6.0, vwap=20.8)
            assert await execution.open_trade(s, _client(), strat, None, "paper", cand, "test entry") is None

    with session_scope() as s:
        row = s.query(Trade).filter(Trade.strategy_id == strategy_row, Trade.status == "rejected").one()
        assert "broker status: pending_new" in row.entry_reason
        assert "canceled" not in row.entry_reason  # our own cancel, not the reason
        assert "filled 0 of 9" in row.entry_reason  # nothing of the 9 shares we asked for


async def test_a_broker_rejection_after_acceptance_is_named_as_such(strategy_row):
    """The other side of the same coin: when Alpaca accepts an order and then
    rejects it, the row must say "rejected" — the case that would send us
    chasing liquidity for a problem that is actually the order itself."""
    # The post-cancel re-read must report something DIFFERENT, or `final` would
    # quietly supply "rejected" too and this would pass without the poll ever
    # having captured it (it did exactly that until a mutation caught it).
    statuses = [
        {"id": "order-10", "status": "rejected", "filled_qty": "0"},
        {"id": "order-10", "status": "canceled", "filled_qty": "0"},
    ]

    with (
        patch.object(AlpacaClient, "submit_order", new=AsyncMock(return_value={"id": "order-10"})),
        patch.object(AlpacaClient, "get_order", new=AsyncMock(side_effect=statuses)),
        patch.object(AlpacaClient, "cancel_order", new=AsyncMock(return_value=None)),
        patch("qt.services.execution.FILL_POLL_SECONDS", (0, 0)),
    ):
        with session_scope() as s:
            strat = s.get(Strategy, strategy_row)
            cand = Candidate(symbol="ROCKET", asset_class="stock", price=21.2, change_pct=6.0, vwap=20.8)
            assert await execution.open_trade(s, _client(), strat, None, "paper", cand, "test entry") is None

    with session_scope() as s:
        row = s.query(Trade).filter(Trade.strategy_id == strategy_row, Trade.status == "rejected").one()
        assert "broker status: rejected" in row.entry_reason


async def test_position_too_small_is_rejected_not_ordered(strategy_row):
    with session_scope() as s:
        strat = s.get(Strategy, strategy_row)
        cand = Candidate(symbol="BRK.A", asset_class="stock", price=700_000.0, change_pct=4.0, vwap=1.0)
        trade = await execution.open_trade(s, _client(), strat, None, "paper", cand, "test entry")
        assert trade is None
    with session_scope() as s:
        rejected = s.query(Trade).filter(Trade.strategy_id == strategy_row, Trade.status == "rejected").one()
        assert "too small" in rejected.entry_reason


_MARKET_PARAMS = '{"entry":{},"exit":{"stop_loss_pct":4},"execution":{"market_orders":true}}'


async def test_market_mode_buys_expensive_name_by_notional(strategy_row):
    # A $200 budget can't buy a whole $700 share, so the default limit path would
    # reject it. Market+fractional sends a NOTIONAL market order and takes a slice.
    submitted = {}

    async def fake_market(self, symbol, side, client_order_id, *, qty=None, notional=None, time_in_force="day"):
        submitted.update(symbol=symbol, side=side, qty=qty, notional=notional, tif=time_in_force)
        return {"id": "m-1", "status": "accepted"}

    filled = {"id": "m-1", "status": "filled", "filled_avg_price": "700.00", "filled_qty": "0.285714"}
    with (
        patch.object(AlpacaClient, "submit_market_order", fake_market),
        patch.object(AlpacaClient, "get_order", new=AsyncMock(return_value=filled)),
        patch("qt.services.execution.FILL_POLL_SECONDS", (0,)),
    ):
        with session_scope() as s:
            strat = s.get(Strategy, strategy_row)
            strat.params = _MARKET_PARAMS
            s.flush()
            cand = Candidate(symbol="BRKB", asset_class="stock", price=700.0, change_pct=5.0, vwap=690.0)
            trade = await execution.open_trade(s, _client(), strat, None, "paper", cand, "expensive name")
            assert trade is not None  # NOT rejected as "too small"
            assert trade.entry_price == 700.0
            assert trade.qty == pytest.approx(0.285714)  # a fractional slice

    assert submitted["side"] == "buy"
    assert submitted["notional"] == 200.0  # the $ per trade, not whole shares
    assert submitted["qty"] is None
    assert submitted["tif"] == "day"


async def test_market_mode_sell_uses_market_order(strategy_row):
    from datetime import datetime, timezone

    submitted = {}

    async def fake_market(self, symbol, side, client_order_id, *, qty=None, notional=None, time_in_force="day"):
        submitted.update(side=side, qty=qty, notional=notional)
        return {"id": "sell-1", "status": "accepted"}

    with session_scope() as s:
        trade = Trade(
            strategy_id=strategy_row, mode="paper", symbol="BRKB", asset_class="stock",
            qty=0.285714, notional=200.0, status="open", entry_price=700.0,
            entry_at=datetime.now(timezone.utc), high_water=710.0,
        )
        s.add(trade)
        s.flush()
        filled = {"id": "sell-1", "status": "filled", "filled_avg_price": "705.00"}
        with (
            patch.object(AlpacaClient, "submit_market_order", fake_market),
            patch.object(AlpacaClient, "get_order", new=AsyncMock(return_value=filled)),
            patch("qt.services.execution.FILL_POLL_SECONDS", (0,)),
        ):
            ok = await execution.close_trade(s, _client(), trade, 705.0, "take-profit", market=True)
        assert ok
        assert trade.status == "closed"
        assert submitted["side"] == "sell"
        assert submitted["qty"] == pytest.approx(0.285714)  # sells the fractional position
        assert submitted["notional"] is None


async def test_cancel_race_adopts_a_late_fill(strategy_row):
    # The poll times out (order still "new"), QT cancels — but the cancel races a
    # late fill (classic crypto GTC). Re-checking must adopt the real position
    # instead of marking it rejected and orphaning it.
    calls = {"n": 0}

    async def fake_get(self, order_id):
        calls["n"] += 1
        if calls["n"] == 1:
            return {"id": "o-9", "status": "new"}
        return {"id": "o-9", "status": "filled", "filled_avg_price": "21.00", "filled_qty": "9"}

    with (
        patch.object(AlpacaClient, "submit_order", new=AsyncMock(return_value={"id": "o-9"})),
        patch.object(AlpacaClient, "get_order", fake_get),
        patch.object(AlpacaClient, "cancel_order", new=AsyncMock(return_value=None)),
        patch("qt.services.execution.FILL_POLL_SECONDS", (0,)),
    ):
        with session_scope() as s:
            strat = s.get(Strategy, strategy_row)
            cand = Candidate(symbol="ROCKET", asset_class="stock", price=21.0, change_pct=6.0, vwap=20.8)
            trade = await execution.open_trade(s, _client(), strat, None, "paper", cand, "race")
            assert trade is not None
            assert trade.status == "open"
            assert trade.qty == 9
            assert trade.entry_price == 21.0


async def test_submit_market_order_requires_exactly_one_size():
    with pytest.raises(ValueError):
        await _client().submit_market_order("X", "buy", "cid", qty=1, notional=5)
    with pytest.raises(ValueError):
        await _client().submit_market_order("X", "buy", "cid")


async def test_paper_sell_records_fill_and_pnl(strategy_row):
    with session_scope() as s:
        from datetime import datetime, timezone

        trade = Trade(
            strategy_id=strategy_row, config_version_id=None, mode="paper", symbol="ROCKET",
            asset_class="stock", qty=9, notional=190.8, status="open",
            entry_price=21.2, entry_at=datetime.now(timezone.utc), high_water=22.0,
        )
        s.add(trade)
        s.flush()

        filled = {"id": "order-3", "status": "filled", "filled_avg_price": "20.05"}
        with (
            patch.object(AlpacaClient, "submit_order", new=AsyncMock(return_value={"id": "order-3"})),
            patch.object(AlpacaClient, "get_order", new=AsyncMock(return_value=filled)),
            patch("qt.services.execution.FILL_POLL_SECONDS", (0,)),
        ):
            ok = await execution.close_trade(s, _client(), trade, 20.1, "stop-loss test")
        assert ok
        assert trade.status == "closed"
        assert trade.exit_price == 20.05
        assert trade.pnl == round((20.05 - 21.2) * 9, 2)
