"""Manual force-exit of a single open position.

The override for when you've decided to be out and don't want to wait for a stop
to be touched. Two properties matter more than the happy path: it must close
exactly ONE trade (another strategy may hold the same symbol and hasn't asked to
be out), and it must not report success when nothing was sold.
"""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest

from qt import security
from qt.broker.alpaca import SECRET_KEY_ID, SECRET_KEY_SECRET, AlpacaClient
from qt.db import session_scope
from qt.models import Strategy, StrategyConfigVersion, Trade
from qt.settings_service import set_setting


@pytest.fixture()
def configured(client):
    with session_scope() as s:
        security.set_secret(s, SECRET_KEY_ID, "k")
        security.set_secret(s, SECRET_KEY_SECRET, "s")
        set_setting(s, "engine_mode", "paper")
    yield
    with session_scope() as s:
        s.query(Trade).delete()
        s.query(StrategyConfigVersion).delete()
        s.query(Strategy).delete()
        set_setting(s, "engine_mode", "off")
        security.delete_secret(s, SECRET_KEY_ID)
        security.delete_secret(s, SECRET_KEY_SECRET)


def _two_strategies_one_symbol(mode: str = "paper") -> tuple[int, int]:
    """Two strategies, both holding NVDA — the case the button must not confuse."""
    with session_scope() as s:
        a = Strategy(name="A", asset_class="stock", universe="custom", preset="custom",
                     params="{}", symbols="[]")
        b = Strategy(name="B", asset_class="stock", universe="custom", preset="custom",
                     params="{}", symbols="[]")
        s.add_all([a, b])
        s.flush()
        rows = [
            Trade(strategy_id=a.id, mode=mode, symbol="NVDA", asset_class="stock", status="open",
                  qty=5, notional=500, entry_price=100.0, entry_at=datetime.now(timezone.utc)),
            Trade(strategy_id=b.id, mode=mode, symbol="NVDA", asset_class="stock", status="open",
                  qty=3, notional=300, entry_price=100.0, entry_at=datetime.now(timezone.utc)),
        ]
        s.add_all(rows)
        s.flush()
        return rows[0].id, rows[1].id


def _status(trade_id: int) -> str:
    with session_scope() as s:
        return s.get(Trade, trade_id).status


def test_it_closes_only_the_position_you_asked_for(client, configured):
    """THE one. Two strategies hold NVDA; closing one must leave the other open."""
    mine, theirs = _two_strategies_one_symbol()
    snap = {"NVDA": {"latestTrade": {"p": 110.0}}}
    with patch.object(AlpacaClient, "stock_snapshots", new=AsyncMock(return_value=snap)), \
         patch("qt.services.execution.close_trade", new=AsyncMock(return_value=True)) as closed:
        r = client.post(f"/api/engine/positions/{mine}/close")
    assert r.status_code == 200 and r.json()["symbol"] == "NVDA"
    assert closed.await_count == 1
    assert closed.await_args.args[2].id == mine, "closed the wrong trade"
    assert _status(theirs) == "open", "the other strategy's position was closed too"


def test_a_rejected_order_is_not_reported_as_closed(client, configured):
    """A 'force exit' that silently didn't sell is the worst possible outcome —
    you'd believe you were flat."""
    mine, _ = _two_strategies_one_symbol()
    with patch.object(AlpacaClient, "stock_snapshots", new=AsyncMock(return_value={})), \
         patch("qt.services.execution.close_trade", new=AsyncMock(return_value=False)):
        r = client.post(f"/api/engine/positions/{mine}/close")
    assert r.status_code == 502
    assert _status(mine) == "open"


def test_it_sells_at_market(client, configured):
    """The escalating marketable limit can sit unfilled. A button that says
    'force' must not leave a resting order behind."""
    mine, _ = _two_strategies_one_symbol()
    with patch.object(AlpacaClient, "stock_snapshots",
                      new=AsyncMock(return_value={"NVDA": {"latestTrade": {"p": 110.0}}})), \
         patch("qt.services.execution.close_trade", new=AsyncMock(return_value=True)) as closed:
        client.post(f"/api/engine/positions/{mine}/close")
    assert closed.await_args.kwargs["market"] is True


def test_a_missing_quote_still_lets_you_out(client, configured):
    """A stale mark is a reason to be careful about the PRICE, never a reason to
    trap someone in a position."""
    mine, _ = _two_strategies_one_symbol()
    with patch.object(AlpacaClient, "stock_snapshots", new=AsyncMock(side_effect=RuntimeError("quotes down"))), \
         patch("qt.services.execution.close_trade", new=AsyncMock(return_value=True)) as closed:
        r = client.post(f"/api/engine/positions/{mine}/close")
    assert r.status_code == 200
    assert closed.await_count == 1


def test_a_shadow_position_closes_the_journal_row_without_an_order(client, configured):
    """Shadow never placed an order, so there's nothing at the broker to sell."""
    mine, _ = _two_strategies_one_symbol(mode="shadow")
    with patch("qt.services.execution.close_trade", new=AsyncMock(return_value=True)) as closed:
        r = client.post(f"/api/engine/positions/{mine}/close")
    assert r.status_code == 200
    assert closed.await_count == 0, "placed a broker order for a shadow position"
    assert _status(mine) == "closed"


def test_closing_an_already_closed_position_is_refused(client, configured):
    mine, _ = _two_strategies_one_symbol()
    with session_scope() as s:
        s.get(Trade, mine).status = "closed"
    r = client.post(f"/api/engine/positions/{mine}/close")
    assert r.status_code == 409


def test_an_unknown_position_is_a_404(client, configured):
    assert client.post("/api/engine/positions/999999/close").status_code == 404
