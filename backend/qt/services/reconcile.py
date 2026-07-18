"""Crash-recovery reconciliation against Alpaca (the source of truth).

If QT is killed mid-flight (e.g. `kill -9` between an order submit and its
confirmation, or while a position is open), the DB journal and the broker can
disagree when we come back up. This module reconciles them.

The decision logic is a PURE function, :func:`reconcile`, that takes plain
views of the DB's open trades and Alpaca's positions/orders and returns a list
of :class:`Action`s. It's exhaustively table-tested with no broker. A thin
impure shell (:func:`apply_reconciliation`) fetches the inputs and applies the
actions to the journal.

Cases handled:
  (a) DB trade open but Alpaca no longer holds the position — the exit
      filled while we were down. Close it in the journal, noting it was
      reconciled, at the last price we knew.
  (b) Alpaca holds a position no open DB trade knows about — log + Slack;
      never auto-adopt (we don't know the strategy/config it belongs to).
  (c) An entry we recorded but never confirmed — check the order: if it
      filled, finalize it; if it's still working, leave it; if it's dead and
      there's no position, mark the trade rejected.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

log = logging.getLogger("qt.reconcile")

# Alpaca order statuses that mean "still working, may yet fill".
WORKING_ORDER_STATES = frozenset(
    {
        "new",
        "accepted",
        "pending_new",
        "accepted_for_bidding",
        "partially_filled",
        "held",
        "pending_replace",
        "replaced",
        "calculated",
    }
)


@dataclass(frozen=True)
class OpenTradeView:
    """What reconcile needs to know about one DB trade we believe is open."""

    id: int
    symbol: str
    qty: float
    entry_order_id: str | None
    entry_confirmed: bool  # did we record a confirmed entry fill?
    last_price: float | None  # best price to book a reconciled close at


@dataclass(frozen=True)
class PositionView:
    symbol: str
    qty: float
    current_price: float | None = None


@dataclass(frozen=True)
class OrderView:
    id: str
    symbol: str
    status: str
    filled_qty: float = 0.0
    filled_avg_price: float | None = None


@dataclass(frozen=True)
class Action:
    kind: str  # confirm_entry | await_entry | reject_entry | close_reconciled | alert_orphan_position
    trade_id: int | None = None
    symbol: str = ""
    qty: float = 0.0
    price: float | None = None
    reason: str = ""


def _nonzero(qty: float) -> bool:
    return abs(qty) > 1e-12


def reconcile(
    trades: list[OpenTradeView],
    positions: list[PositionView],
    orders: list[OrderView],
) -> list[Action]:
    """Pure reconciliation. Returns the actions the shell should apply."""
    pos_by_symbol = {p.symbol: p for p in positions if _nonzero(p.qty)}
    orders_by_id = {o.id: o for o in orders}
    db_symbols = {t.symbol for t in trades}
    actions: list[Action] = []

    for t in trades:
        pos = pos_by_symbol.get(t.symbol)
        entry_order = orders_by_id.get(t.entry_order_id) if t.entry_order_id else None

        if not t.entry_confirmed:
            # Case (c): we recorded the trade but never confirmed its entry.
            if entry_order is not None and entry_order.status == "filled":
                actions.append(
                    Action(
                        "confirm_entry", t.id, t.symbol,
                        qty=entry_order.filled_qty or t.qty,
                        price=entry_order.filled_avg_price,
                        reason="reconciled: entry order filled while QT was down",
                    )
                )
                continue
            if pos is not None:
                # No confirmed fill recorded, but the broker holds the position:
                # it did fill. Adopt the broker's truth for this known trade.
                actions.append(
                    Action(
                        "confirm_entry", t.id, t.symbol,
                        qty=pos.qty, price=pos.current_price,
                        reason="reconciled: position present though entry was unconfirmed",
                    )
                )
                continue
            if entry_order is not None and entry_order.status in WORKING_ORDER_STATES:
                actions.append(
                    Action(
                        "await_entry", t.id, t.symbol,
                        reason="entry order still working at broker — leaving trade open",
                    )
                )
                continue
            # No position, entry order dead or unknown -> the entry never took.
            actions.append(
                Action(
                    "reject_entry", t.id, t.symbol,
                    reason="reconciled: entry never filled (no position, order not working)",
                )
            )
            continue

        # entry_confirmed: we believe we hold this position.
        if pos is not None:
            continue  # in sync — nothing to do
        # Case (a): the position is gone, so the exit filled while we were down.
        actions.append(
            Action(
                "close_reconciled", t.id, t.symbol,
                price=t.last_price,
                reason="reconciled: position no longer held at broker",
            )
        )

    # Case (b): positions Alpaca holds that no DB trade accounts for.
    for symbol, pos in pos_by_symbol.items():
        if symbol not in db_symbols:
            actions.append(
                Action(
                    "alert_orphan_position", symbol=symbol, qty=pos.qty,
                    reason="Alpaca holds a position QT has no open trade for — not auto-adopting",
                )
            )

    return actions


# ---------------------------------------------------------------------------
# Impure shell: gather inputs from the DB + broker, apply the actions.
# ---------------------------------------------------------------------------


def _to_float(value, default: float | None = None) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


async def apply_reconciliation(session, client, mode: str) -> list[Action]:
    """Reconcile the DB's open trades for ``mode`` against Alpaca and apply the
    resulting journal fixes. Returns the actions taken (for logging/tests).

    Best-effort: any broker error aborts without touching the journal — a
    reconciliation we can't trust must never mutate state."""
    from datetime import datetime, timezone

    from qt.models import AuditLog, Trade
    from qt.services import notify

    open_trades = (
        session.query(Trade).filter(Trade.mode == mode, Trade.status == "open").all()
    )
    # Even with no open trades we still check for orphan broker positions.
    try:
        raw_positions = await client.list_positions()
        raw_orders = await client.list_orders(status="open")
    except Exception as exc:  # AlpacaError or network
        log.warning("reconciliation skipped: broker unreachable: %s", exc)
        return []

    trades_by_id = {t.id: t for t in open_trades}
    views = [
        OpenTradeView(
            id=t.id,
            symbol=t.symbol,
            qty=t.qty,
            entry_order_id=t.entry_order_id,
            entry_confirmed=t.entry_price is not None and t.entry_order_id is not None,
            last_price=t.high_water or t.entry_price,
        )
        for t in open_trades
    ]
    positions = [
        PositionView(
            symbol=p.get("symbol", ""),
            qty=_to_float(p.get("qty"), 0.0) or 0.0,
            current_price=_to_float(p.get("current_price")),
        )
        for p in raw_positions
    ]
    orders = [
        OrderView(
            id=o.get("id", ""),
            symbol=o.get("symbol", ""),
            status=o.get("status", ""),
            filled_qty=_to_float(o.get("filled_qty"), 0.0) or 0.0,
            filled_avg_price=_to_float(o.get("filled_avg_price")),
        )
        for o in raw_orders
    ]

    actions = reconcile(views, positions, orders)
    now = datetime.now(timezone.utc)

    for action in actions:
        trade = trades_by_id.get(action.trade_id) if action.trade_id else None

        if action.kind == "close_reconciled" and trade is not None:
            price = action.price or trade.entry_price or 0.0
            trade.exit_price = price
            trade.exit_at = now
            trade.exit_reason = action.reason
            trade.pnl = round((price - (trade.entry_price or price)) * trade.qty, 2)
            trade.status = "closed"
            session.add(AuditLog(
                category="reconcile",
                message=f"[{mode}] reconciled CLOSE {trade.qty:g} {trade.symbol} @ ~${price:,.4f}",
                detail=action.reason,
            ))
            await notify.slack(
                session,
                f":arrows_counterclockwise: *{mode.upper()}* reconciled {trade.symbol}: "
                f"{action.reason} (booked exit ~${price:,.4f}).",
            )

        elif action.kind == "confirm_entry" and trade is not None:
            if action.price is not None:
                trade.entry_price = action.price
            if action.qty:
                trade.qty = action.qty
            trade.notional = (trade.entry_price or 0.0) * trade.qty
            trade.high_water = trade.entry_price
            trade.status = "open"
            session.add(AuditLog(
                category="reconcile",
                message=f"[{mode}] reconciled ENTRY {trade.symbol} confirmed filled",
                detail=action.reason,
            ))

        elif action.kind == "reject_entry" and trade is not None:
            trade.status = "rejected"
            trade.entry_reason = f"{trade.entry_reason} | {action.reason}".strip(" |")
            session.add(AuditLog(
                category="reconcile",
                message=f"[{mode}] reconciled {trade.symbol}: entry never filled",
                detail=action.reason,
            ))

        elif action.kind == "await_entry":
            log.info("reconcile: %s %s", action.symbol, action.reason)

        elif action.kind == "alert_orphan_position":
            session.add(AuditLog(
                category="reconcile",
                message=f"[{mode}] ORPHAN position at broker: {action.qty:g} {action.symbol}",
                detail=action.reason,
            ))
            await notify.slack(
                session,
                f":warning: *{mode.upper()}* Alpaca holds {action.qty:g} {action.symbol} that QT "
                "has no open trade for. Not auto-adopting — check manually.",
            )

    if actions:
        log.info("reconciliation applied %d action(s) in %s mode", len(actions), mode)
    return actions
