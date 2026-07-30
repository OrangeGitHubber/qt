"""Account-wide open-positions view: every open trade across ALL strategies
with its owner named — the answer to "which strategy already holds the symbol
that blocked my entry?". Mixed asset classes mark from one batched snapshot
call per class; closed trades never appear."""

from unittest.mock import AsyncMock, patch

import pytest

from qt import security
from qt.broker.alpaca import SECRET_KEY_ID, SECRET_KEY_SECRET, AlpacaClient
from qt.db import session_scope
from qt.models import Strategy, Trade


@pytest.fixture()
def seeded(client):
    with session_scope() as s:
        security.set_secret(s, SECRET_KEY_ID, "k")
        security.set_secret(s, SECRET_KEY_SECRET, "s")
        a = Strategy(name="Alpha", asset_class="stock", universe="scanner", params='{"entry": {}, "exit": {}}')
        b = Strategy(name="Beta", asset_class="crypto", universe="custom", params='{"entry": {}, "exit": {}}')
        s.add_all([a, b])
        s.flush()
        s.add(Trade(strategy_id=a.id, mode="paper", symbol="AAPL", asset_class="stock",
                    status="open", qty=2, notional=200, entry_price=100.0))
        s.add(Trade(strategy_id=b.id, mode="paper", symbol="SOL/USD", asset_class="crypto",
                    status="open", qty=5, notional=350, entry_price=70.0))
        s.add(Trade(strategy_id=a.id, mode="paper", symbol="MSFT", asset_class="stock",
                    status="closed", qty=1, notional=100, entry_price=50.0))
        s.commit()
    yield
    with session_scope() as s:
        s.query(Trade).delete()
        s.query(Strategy).delete()
        security.delete_secret(s, SECRET_KEY_ID)
        security.delete_secret(s, SECRET_KEY_SECRET)


def test_positions_list_all_open_trades_with_their_owner(client, seeded):
    with (
        patch.object(AlpacaClient, "stock_snapshots",
                     new=AsyncMock(return_value={"AAPL": {"dailyBar": {"c": 110.0}}})),
        patch.object(AlpacaClient, "crypto_snapshots",
                     new=AsyncMock(return_value={"SOL/USD": {"dailyBar": {"c": 73.5}}})),
    ):
        body = client.get("/api/engine/positions").json()
    rows = body["positions"]
    assert len(rows) == 2  # the closed MSFT trade is excluded
    by_symbol = {r["symbol"]: r for r in rows}
    assert by_symbol["AAPL"]["strategy_name"] == "Alpha"
    assert by_symbol["SOL/USD"]["strategy_name"] == "Beta"
    assert by_symbol["AAPL"]["unrealized_pnl"] == 20.0  # (110 − 100) × 2
    assert by_symbol["SOL/USD"]["unrealized_pnl"] == 17.5  # (73.5 − 70) × 5
    assert body["total_unrealized_pnl"] == 37.5


def test_positions_degrade_to_entry_data_on_price_hiccup(client, seeded):
    boom = AsyncMock(side_effect=Exception("alpaca down"))
    with (
        patch.object(AlpacaClient, "stock_snapshots", new=boom),
        patch.object(AlpacaClient, "crypto_snapshots", new=boom),
    ):
        body = client.get("/api/engine/positions").json()
    assert len(body["positions"]) == 2  # rows survive; marks are just missing
    assert all(r["current_price"] is None and r["unrealized_pnl"] is None for r in body["positions"])
    assert body["total_cost"] == 550.0  # entry data still totals
