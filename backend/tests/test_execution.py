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


async def test_position_too_small_is_rejected_not_ordered(strategy_row):
    with session_scope() as s:
        strat = s.get(Strategy, strategy_row)
        cand = Candidate(symbol="BRK.A", asset_class="stock", price=700_000.0, change_pct=4.0, vwap=1.0)
        trade = await execution.open_trade(s, _client(), strat, None, "paper", cand, "test entry")
        assert trade is None
    with session_scope() as s:
        rejected = s.query(Trade).filter(Trade.strategy_id == strategy_row, Trade.status == "rejected").one()
        assert "too small" in rejected.entry_reason


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
