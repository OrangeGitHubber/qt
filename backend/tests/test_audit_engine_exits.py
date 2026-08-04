"""The exit path is the one QT cannot re-run: a position it fails to sell keeps
losing money while the tick that should have sold it retries.

Four faults, all of them shapes that let a sell order be wrong about its own
size, and every one of them ending in the same place — an order the broker must
refuse, resubmitted every 60 seconds with no branch that ever stops.
"""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest

from qt.broker.alpaca import AlpacaClient
from qt.db import session_scope
from qt.models import AuditLog, Strategy, Trade
from qt.services import execution


@pytest.fixture()
def strategy_row():
    with session_scope() as s:
        strat = Strategy(
            name="exit audit", enabled=True, asset_class="crypto", universe="scanner",
            preset="custom", params='{"entry":{},"exit":{"stop_loss_pct":4}}',
            sizing_usd=200, sleeve_usd=1000, max_positions=3, swing_mode=False,
            ignore_regime=False,
        )
        s.add(strat)
        s.flush()
        sid = strat.id
    yield sid
    with session_scope() as s:
        s.query(Trade).filter(Trade.strategy_id == sid).delete()
        s.query(Strategy).filter(Strategy.id == sid).delete()
    execution._exit_attempts.clear()


def _client() -> AlpacaClient:
    return AlpacaClient(key_id="k", key_secret="s")


def _open_crypto(session, sid: int, qty: float, entry: float = 100.0) -> Trade:
    trade = Trade(
        strategy_id=sid, mode="paper", symbol="AAVE/USD", asset_class="crypto",
        qty=qty, notional=qty * entry, status="open", entry_price=entry,
        entry_at=datetime.now(timezone.utc), high_water=entry,
    )
    session.add(trade)
    session.flush()
    return trade


# --------------------------------------------------------------------------
# 1. The crypto fee is paid IN COIN, so the journal is knowingly above the
#    broker for up to the 15 minutes until reconcile runs — while exits run
#    every 60 seconds.
# --------------------------------------------------------------------------


async def test_a_crypto_exit_never_orders_more_than_the_broker_holds(strategy_row):
    """Alpaca takes crypto commission out of the coin it delivers, so a journal
    that records the ORDER's filled_qty sits ~0.25% above the position that
    actually landed. reconcile squares them every 15 minutes; the exit tick runs
    every 60 seconds. In between, the sell asks for coins the account does not
    have, Alpaca refuses it, and the stop-loss simply cannot fire."""
    submitted = {}

    async def fake_submit(self, symbol, qty, side, limit_price, client_order_id, time_in_force="day"):
        submitted.update(qty=qty)
        return {"id": "s-1", "status": "accepted"}

    # Exactly the shortfall reconcile.py records observing: QT 2.1322 against
    # the broker's 2.12687.
    positions = [{"symbol": "AAVEUSD", "qty": "2.12687"}]
    filled = {"id": "s-1", "status": "filled", "filled_avg_price": "99.0", "filled_qty": "2.12687"}
    with (
        patch.object(AlpacaClient, "submit_order", fake_submit),
        patch.object(AlpacaClient, "get_order", new=AsyncMock(return_value=filled)),
        patch.object(AlpacaClient, "list_positions", new=AsyncMock(return_value=positions)),
        patch("qt.services.execution.FILL_POLL_SECONDS", (0,)),
    ):
        with session_scope() as s:
            trade = _open_crypto(s, strategy_row, 2.1322)
            ok = await execution.close_trade(s, _client(), trade, 99.0, "stop-loss")
            assert ok
            journal_qty = trade.qty

    assert submitted["qty"] == pytest.approx(2.12687), (
        "the sell asked for more coins than the account holds — Alpaca refuses it "
        "and the position cannot be exited until reconcile runs"
    )
    assert journal_qty == pytest.approx(2.12687)


async def test_the_fee_correction_sticks_even_when_the_exit_does_not_fill(strategy_row):
    """Separate claim from the one above, and the one that matters most: the
    journal is squared with the broker BEFORE the order goes out, so a miss
    leaves behind a corrected quantity. Corrected only on the way through a
    successful fill, the next tick would size the retry off the stale figure and
    miss for exactly the same reason, over and over."""
    positions = [{"symbol": "AAVEUSD", "qty": "2.12687"}]
    resting = {"id": "s-3", "status": "new", "filled_qty": "0"}
    with (
        patch.object(AlpacaClient, "submit_order", new=AsyncMock(return_value={"id": "s-3"})),
        patch.object(AlpacaClient, "get_order", new=AsyncMock(return_value=resting)),
        patch.object(AlpacaClient, "cancel_order", new=AsyncMock(return_value=None)),
        patch.object(AlpacaClient, "list_positions", new=AsyncMock(return_value=positions)),
        patch("qt.services.execution.FILL_POLL_SECONDS", (0,)),
    ):
        with session_scope() as s:
            trade = _open_crypto(s, strategy_row, 2.1322)
            assert await execution.close_trade(s, _client(), trade, 99.0, "stop-loss") is False
            assert trade.status == "open"
            left_behind = trade.qty

    assert left_behind == pytest.approx(2.12687), (
        "the exit missed and the journal still claims coins the account does not "
        "hold, so every retry is sized to fail the same way"
    )


async def test_an_unexplained_shortfall_clamps_the_order_but_not_the_journal(strategy_row):
    """A gap far too big to be the fee is NOT the fee. reconcile deliberately
    refuses to auto-correct drift it cannot attribute, and so must this: refuse
    to order coins that do not exist, but leave the journal for reconcile to
    alert on rather than quietly writing off half a position."""
    submitted = {}

    async def fake_submit(self, symbol, qty, side, limit_price, client_order_id, time_in_force="day"):
        submitted.update(qty=qty)
        return {"id": "s-2", "status": "accepted"}

    positions = [{"symbol": "AAVEUSD", "qty": "5"}]  # we think we hold 10
    filled = {"id": "s-2", "status": "filled", "filled_avg_price": "99.0", "filled_qty": "5"}
    with (
        patch.object(AlpacaClient, "submit_order", fake_submit),
        patch.object(AlpacaClient, "get_order", new=AsyncMock(return_value=filled)),
        patch.object(AlpacaClient, "list_positions", new=AsyncMock(return_value=positions)),
        patch("qt.services.execution.FILL_POLL_SECONDS", (0,)),
    ):
        with session_scope() as s:
            trade = _open_crypto(s, strategy_row, 10.0)
            await execution.close_trade(s, _client(), trade, 99.0, "stop-loss")
            booked_qty = trade.qty

    assert submitted["qty"] == pytest.approx(5)
    # The P&L is booked on the 5 that actually sold, not the 10 we claimed.
    assert booked_qty == pytest.approx(5)


# --------------------------------------------------------------------------
# 2. The give-up branch that did not exist.
# --------------------------------------------------------------------------


async def test_dust_below_alpacas_minimum_is_never_submitted(strategy_row):
    """A part-filled entry or a fee haircut can leave a position worth cents.
    Alpaca will not accept an order under about a dollar, so submitting it is not
    a retry — it is a loop, run once a minute for as long as the trade is open."""
    calls = []

    async def fake_submit(self, symbol, qty, side, limit_price, client_order_id, time_in_force="day"):
        calls.append(qty)
        return {"id": "never", "status": "accepted"}

    positions = [{"symbol": "AAVEUSD", "qty": "0.000004"}]
    with (
        patch.object(AlpacaClient, "submit_order", fake_submit),
        # Only reached if the floor fails to hold — pinned so the test then fails
        # on its own assertion rather than on an unmocked network call.
        patch.object(AlpacaClient, "get_order",
                     new=AsyncMock(return_value={"id": "never", "status": "canceled", "filled_qty": "0"})),
        patch.object(AlpacaClient, "cancel_order", new=AsyncMock(return_value=None)),
        patch.object(AlpacaClient, "list_positions", new=AsyncMock(return_value=positions)),
        patch("qt.services.execution.FILL_POLL_SECONDS", (0,)),
    ):
        with session_scope() as s:
            trade = _open_crypto(s, strategy_row, 0.000004)
            # 0.000004 x $100 = $0.0004 — four hundredths of a cent.
            ok = await execution.close_trade(s, _client(), trade, 100.0, "stop-loss")
            assert ok is False

    assert calls == [], "an order Alpaca must reject was still submitted"
    with session_scope() as s:
        rows = [
            a for a in s.query(AuditLog).filter(AuditLog.category == "trade").all()
            if "NOT SUBMITTED" in (a.message or "")
        ]
    assert rows, "QT gave up silently — the stuck position was never reported"


async def test_a_position_the_broker_no_longer_holds_is_not_re_ordered(strategy_row):
    """The exit already happened (a race, or a manual close at the broker). There
    is nothing left to sell, so there is nothing to retry — reconcile closes the
    journal row. Ordering a sale of zero coins every minute helps nobody."""
    calls = []

    async def fake_submit(self, symbol, qty, side, limit_price, client_order_id, time_in_force="day"):
        calls.append(qty)
        return {"id": "never", "status": "accepted"}

    with (
        patch.object(AlpacaClient, "submit_order", fake_submit),
        patch.object(AlpacaClient, "get_order",
                     new=AsyncMock(return_value={"id": "never", "status": "canceled", "filled_qty": "0"})),
        patch.object(AlpacaClient, "cancel_order", new=AsyncMock(return_value=None)),
        patch.object(AlpacaClient, "list_positions", new=AsyncMock(return_value=[])),
        patch("qt.services.execution.FILL_POLL_SECONDS", (0,)),
    ):
        with session_scope() as s:
            trade = _open_crypto(s, strategy_row, 3.0)
            assert await execution.close_trade(s, _client(), trade, 100.0, "stop-loss") is False

    assert calls == []


# --------------------------------------------------------------------------
# 3 & 4. What the exit poll learned and then threw away.
# --------------------------------------------------------------------------


async def test_a_repeatedly_refused_sell_is_eventually_reported(strategy_row):
    """Retrying is right — liquidity comes back and abandoning a real position is
    worse. Silence is not: before this, a sell Alpaca refused on every one of the
    1,440 ticks in a day produced 1,440 audit lines and not one alert."""
    from qt.broker.alpaca import AlpacaError

    alerts = []

    async def fake_slack(session, category, text):
        alerts.append(text)
        return True

    async def always_refuse(self, *a, **k):
        raise AlpacaError(403, "insufficient balance")

    with (
        patch.object(AlpacaClient, "submit_order", always_refuse),
        patch.object(AlpacaClient, "list_positions",
                     new=AsyncMock(return_value=[{"symbol": "AAVEUSD", "qty": "10"}])),
        patch("qt.services.execution.notify.slack_cat", fake_slack),
        patch("qt.services.execution.FILL_POLL_SECONDS", (0,)),
    ):
        with session_scope() as s:
            trade = _open_crypto(s, strategy_row, 10.0)
            for _ in range(execution.EXIT_ALERT_AFTER_ATTEMPTS + 3):
                assert await execution.close_trade(s, _client(), trade, 100.0, "stop-loss") is False

    assert len(alerts) == 1, (
        "a position that cannot be sold was either never reported, or reported on "
        f"every single tick (got {len(alerts)} alerts)"
    )
    assert "failed to exit" in alerts[0]


async def test_a_part_filled_exit_shrinks_the_journal_to_the_remainder(strategy_row):
    """close_trade carried a comment claiming GTC protected it from this. It does
    not: a resting limit part-fills exactly as readily as an IOC, and we cancel
    after ~6s either way. The units are gone from the account whatever the
    journal says — left whole, the next tick orders the full size again, the
    broker refuses it for want of coins, and that repeats forever."""
    order_states = [
        {"id": "p-1", "status": "new", "filled_qty": "0"},          # the poll
        {"id": "p-1", "status": "canceled", "filled_qty": "4",      # the re-read
         "filled_avg_price": "110.0"},
    ]

    with (
        patch.object(AlpacaClient, "submit_order", new=AsyncMock(return_value={"id": "p-1"})),
        patch.object(AlpacaClient, "get_order", new=AsyncMock(side_effect=order_states)),
        patch.object(AlpacaClient, "cancel_order", new=AsyncMock(return_value=None)),
        patch.object(AlpacaClient, "list_positions",
                     new=AsyncMock(return_value=[{"symbol": "AAVEUSD", "qty": "10"}])),
        patch("qt.services.execution.FILL_POLL_SECONDS", (0,)),
    ):
        with session_scope() as s:
            trade = _open_crypto(s, strategy_row, 10.0)
            ok = await execution.close_trade(s, _client(), trade, 100.0, "stop-loss")
            assert ok is False          # still holding some — not a completed exit
            assert trade.status == "open"
            remaining = trade.qty

    assert remaining == pytest.approx(6), (
        "the 4 units that really sold were discarded, so the next cycle would "
        "order all 10 again against a position of 6"
    )


async def test_an_exit_that_filled_during_the_cancel_race_is_adopted(strategy_row):
    """open_trade has re-read the order after cancelling since the RENDER
    incident, because a cancel can race a fill. The EXIT side never did: it
    cancelled, called the exit a miss, and left the journal claiming a position
    the broker had already sold."""
    order_states = [
        {"id": "r-1", "status": "new", "filled_qty": "0"},
        {"id": "r-1", "status": "filled", "filled_qty": "10", "filled_avg_price": "112.5"},
    ]

    with (
        patch.object(AlpacaClient, "submit_order", new=AsyncMock(return_value={"id": "r-1"})),
        patch.object(AlpacaClient, "get_order", new=AsyncMock(side_effect=order_states)),
        patch.object(AlpacaClient, "cancel_order", new=AsyncMock(return_value=None)),
        patch.object(AlpacaClient, "list_positions",
                     new=AsyncMock(return_value=[{"symbol": "AAVEUSD", "qty": "10"}])),
        patch("qt.services.execution.FILL_POLL_SECONDS", (0,)),
    ):
        with session_scope() as s:
            trade = _open_crypto(s, strategy_row, 10.0)
            ok = await execution.close_trade(s, _client(), trade, 100.0, "take-profit")
            assert ok is True
            assert trade.status == "closed"
            # The real average, not the tick price we asked against.
            assert trade.exit_price == pytest.approx(112.5)
