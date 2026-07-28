"""Manual broker actions: liquidate the whole account and reconcile QT's trades."""

from unittest.mock import AsyncMock, patch

import pytest

from qt import security
from qt.broker.alpaca import SECRET_KEY_ID, SECRET_KEY_SECRET, AlpacaClient
from qt.db import session_scope
from qt.models import Strategy, Trade


@pytest.fixture()
def configured(client):
    with session_scope() as s:
        security.set_secret(s, SECRET_KEY_ID, "k")
        security.set_secret(s, SECRET_KEY_SECRET, "s")
    yield
    with session_scope() as s:
        security.delete_secret(s, SECRET_KEY_ID)
        security.delete_secret(s, SECRET_KEY_SECRET)


def test_liquidate_closes_positions_and_reconciles(client, configured):
    with session_scope() as s:
        s.query(Trade).delete()
        s.query(Strategy).delete()
        strat = Strategy(name="Liq", asset_class="stock", universe="scanner", params='{"entry": {}, "exit": {}}')
        s.add(strat)
        s.flush()
        s.add(Trade(strategy_id=strat.id, mode="paper", symbol="AAPL", asset_class="stock",
                    status="open", qty=2, notional=200, entry_price=100.0))
        # A shadow open trade is hypothetical (no broker position) — must be left alone.
        s.add(Trade(strategy_id=strat.id, mode="shadow", symbol="MSFT", asset_class="stock",
                    status="open", qty=1, notional=100, entry_price=50.0))
        s.commit()

    positions = [
        {"symbol": "AAPL", "current_price": "150"},
        {"symbol": "ETHUSD", "current_price": "3000"},  # an orphan QT never tracked
    ]
    close_result = [{"symbol": "AAPL", "status": 200}, {"symbol": "ETHUSD", "status": 200}]
    with patch.object(AlpacaClient, "list_positions", new=AsyncMock(return_value=positions)), \
         patch.object(AlpacaClient, "close_all_positions", new=AsyncMock(return_value=close_result)):
        resp = client.post("/api/broker/liquidate")

    assert resp.status_code == 200
    body = resp.json()
    assert body["positions_closed"] == 2
    assert body["trades_reconciled"] == 1  # the paper trade only — not the shadow one
    assert body["orphans_cleared"] == ["ETHUSD"]

    with session_scope() as s:
        aapl = s.query(Trade).filter(Trade.symbol == "AAPL").one()
        assert aapl.status == "closed"
        assert aapl.exit_price == 150.0
        assert aapl.pnl == 100.0  # (150 - 100) * 2
        assert "liquidation" in aapl.exit_reason
        msft = s.query(Trade).filter(Trade.symbol == "MSFT").one()
        assert msft.status == "open"  # shadow trade untouched
        s.query(Trade).delete()
        s.query(Strategy).delete()
        s.commit()


def test_liquidate_needs_alpaca_configured(client):
    # No keys saved -> the endpoint's require_client dependency should reject it.
    assert client.post("/api/broker/liquidate").status_code in (400, 401, 409, 422, 503)
