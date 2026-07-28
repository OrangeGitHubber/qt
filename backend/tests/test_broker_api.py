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


def _seed_trades():
    with session_scope() as s:
        s.query(Trade).delete()
        s.query(Strategy).delete()
        strat = Strategy(name="Liq", asset_class="stock", universe="scanner", params='{"entry": {}, "exit": {}}')
        s.add(strat)
        s.flush()
        s.add(Trade(strategy_id=strat.id, mode="paper", symbol="AAPL", asset_class="stock",
                    status="open", qty=2, notional=200, entry_price=100.0))
        # A crypto trade stored slash-form; the broker returns it slash-less.
        s.add(Trade(strategy_id=strat.id, mode="paper", symbol="AVAX/USD", asset_class="crypto",
                    status="open", qty=5, notional=100, entry_price=20.0))
        # A shadow trade is hypothetical (no broker position) — must be left alone.
        s.add(Trade(strategy_id=strat.id, mode="shadow", symbol="MSFT", asset_class="stock",
                    status="open", qty=1, notional=100, entry_price=50.0))
        s.commit()


def _cleanup():
    with session_scope() as s:
        s.query(Trade).delete()
        s.query(Strategy).delete()
        s.commit()


# Broker positions: QT's two (AAPL, AVAXUSD) plus an orphan (ETHUSD) QT never opened.
POSITIONS = [
    {"symbol": "AAPL", "qty": "2", "current_price": "150"},
    {"symbol": "AVAXUSD", "qty": "5", "current_price": "25"},
    {"symbol": "ETHUSD", "qty": "10", "current_price": "3000"},
]


def test_liquidate_qt_only_leaves_orphans(client, configured):
    _seed_trades()
    close_pos = AsyncMock(return_value={})
    with patch.object(AlpacaClient, "list_positions", new=AsyncMock(return_value=POSITIONS)), \
         patch.object(AlpacaClient, "close_position", new=close_pos), \
         patch.object(AlpacaClient, "close_all_positions", new=AsyncMock(side_effect=AssertionError("must not flatten all"))):
        body = client.post("/api/broker/liquidate", json={"include_orphans": False}).json()

    assert body["mode"] == "qt_only"
    assert body["positions_closed"] == 2  # AAPL + AVAXUSD, not the orphan
    assert body["trades_reconciled"] == 2  # both paper trades, not shadow
    assert body["orphans_left"] == ["ETHUSD"]
    assert body["orphans_cleared"] == []
    # It closed exactly QT's symbols (broker forms), and never touched ETHUSD.
    closed_symbols = sorted(c.args[0] for c in close_pos.call_args_list)
    assert closed_symbols == ["AAPL", "AVAXUSD"]

    with session_scope() as s:
        assert s.query(Trade).filter(Trade.symbol == "AAPL").one().pnl == 100.0  # (150-100)*2
        assert s.query(Trade).filter(Trade.symbol == "MSFT").one().status == "open"  # shadow untouched
    _cleanup()


def test_liquidate_include_orphans_flattens_everything(client, configured):
    _seed_trades()
    close_all = AsyncMock(return_value=[{"symbol": p["symbol"]} for p in POSITIONS])
    with patch.object(AlpacaClient, "list_positions", new=AsyncMock(return_value=POSITIONS)), \
         patch.object(AlpacaClient, "close_all_positions", new=close_all):
        body = client.post("/api/broker/liquidate", json={"include_orphans": True}).json()

    assert body["mode"] == "full"
    assert body["positions_closed"] == 3
    assert body["orphans_cleared"] == ["ETHUSD"]
    assert body["orphans_left"] == []
    _cleanup()


def test_liquidate_needs_alpaca_configured(client):
    # No keys saved -> the endpoint's require_client dependency should reject it.
    assert client.post("/api/broker/liquidate", json={}).status_code in (400, 401, 409, 422, 503)
