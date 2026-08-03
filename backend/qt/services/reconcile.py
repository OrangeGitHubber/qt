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
  (d) The broker's quantity for a symbol doesn't match what our open trades add
      up to — log + Slack, never auto-correct.

QUANTITY AWARENESS. The broker reports ONE net position per symbol, so once two
strategies may hold the same name (see Strategy.allow_concurrent_symbol) a
symbol match is no longer an identity match. Two things had to change:

* An unconfirmed entry adopted ``pos.qty`` — the WHOLE position. With a second
  strategy already holding the symbol, that trade would claim the other's shares
  too and the journal would total more than the broker holds. It now adopts the
  broker's figure only when a single trade owns the symbol; otherwise it trusts
  the fill it can actually attribute.
* "In sync" meant "a position exists". It now means the open trades for that
  symbol SUM to what the broker holds, and a mismatch is reported.

Case (a) is unchanged and still correct for many trades: if the broker holds
none of a symbol, nobody holds any of it.
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
    asset_class: str = "stock"  # crypto pays its fee IN THE COIN — see case (d)


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
    kind: str  # confirm_entry | await_entry | reject_entry | close_reconciled |
    #            alert_orphan_position | alert_qty_mismatch |
    #            adjust_qty_fee_in_kind
    trade_id: int | None = None
    symbol: str = ""
    qty: float = 0.0
    price: float | None = None
    reason: str = ""


# Alpaca charges crypto commission IN THE COIN it delivers, so the position that
# lands is smaller than the order's own filled_qty by the fee rate. QT journals
# filled_qty, so every crypto entry leaves the journal reading ~0.25% above the
# broker — real, expected, and not drift. Observed exactly: QT 2.1322 AAVE/USD
# against the broker's 2.12687, a shortfall of 0.249977%.
#
# The band is deliberately a little above Alpaca's top tier so a fee change or a
# second fee-bearing fill stays inside it, and deliberately far below anything a
# genuine mis-journalled position would look like.
CRYPTO_FEE_IN_KIND_MAX_PCT = 0.5


def _nonzero(qty: float) -> bool:
    return abs(qty) > 1e-12


def _norm(symbol: str) -> str:
    """Canonical symbol key for matching. We store crypto as 'AVAX/USD', but
    Alpaca's /v2/positions returns it slash-less ('AVAXUSD'). Without this,
    every crypto position looks 'no longer held' on the next reconcile cycle
    and the trade is wrongly closed at entry price (P&L $0)."""
    return symbol.replace("/", "").upper()


def reconcile(
    trades: list[OpenTradeView],
    positions: list[PositionView],
    orders: list[OrderView],
) -> list[Action]:
    """Pure reconciliation. Returns the actions the shell should apply."""
    pos_by_symbol = {_norm(p.symbol): p for p in positions if _nonzero(p.qty)}
    orders_by_id = {o.id: o for o in orders}
    db_symbols = {_norm(t.symbol) for t in trades}
    # A symbol can now be held by more than one strategy, so "which trade does
    # this position belong to?" has no single answer — group them and reason
    # about the total.
    trades_by_symbol: dict[str, list[OpenTradeView]] = {}
    for t in trades:
        trades_by_symbol.setdefault(_norm(t.symbol), []).append(t)
    actions: list[Action] = []

    for t in trades:
        pos = pos_by_symbol.get(_norm(t.symbol))
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
                # it did fill. Adopt the broker's quantity ONLY when this trade is
                # the sole claimant — otherwise pos.qty includes another
                # strategy's shares and adopting it would double-count them.
                sole = len(trades_by_symbol[_norm(t.symbol)]) == 1
                actions.append(
                    Action(
                        "confirm_entry", t.id, t.symbol,
                        qty=pos.qty if sole else t.qty,
                        price=pos.current_price,
                        reason=(
                            "reconciled: position present though entry was unconfirmed"
                            if sole
                            else "reconciled: position present though entry was unconfirmed "
                                 "(kept this trade's own quantity — the symbol is shared)"
                        ),
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
            continue  # the symbol is held; the totals are checked once, below
        # Case (a): the position is gone, so the exit filled while we were down.
        actions.append(
            Action(
                "close_reconciled", t.id, t.symbol,
                price=t.last_price,
                reason="reconciled: position no longer held at broker",
            )
        )

    # Case (d): the broker holds this symbol, but not the amount our open trades
    # claim between them. Reported, never auto-corrected: we can't tell WHICH
    # strategy drifted, and guessing would either invent shares or write off real
    # ones. Only confirmed entries count — an unconfirmed one is handled above.
    for norm_symbol, group in trades_by_symbol.items():
        pos = pos_by_symbol.get(norm_symbol)
        if pos is None:
            continue
        ours = sum(t.qty for t in group if t.entry_confirmed)
        if not _nonzero(ours):
            continue
        if abs(ours - pos.qty) > max(1e-6, abs(pos.qty) * 1e-6):
            # The crypto fee-in-kind case, told apart from real drift by three
            # things at once: crypto, a SHORTFALL (never a surplus — a fee cannot
            # give coins back), and a size inside the fee band. Unlike general
            # drift this IS attributable: every fill in the group paid it, in
            # proportion to its own size, so scaling each trade down to match the
            # broker restores the truth rather than guessing at it.
            shortfall_pct = (ours - pos.qty) / ours * 100 if ours else 0.0
            all_crypto = all(t.asset_class == "crypto" for t in group)
            if all_crypto and 0 < shortfall_pct <= CRYPTO_FEE_IN_KIND_MAX_PCT:
                for t in group:
                    if not t.entry_confirmed:
                        continue
                    actions.append(
                        Action(
                            "adjust_qty_fee_in_kind", t.id, t.symbol,
                            qty=t.qty * (pos.qty / ours),
                            reason=(
                                f"crypto fee taken in coin: {shortfall_pct:.3f}% of "
                                f"{t.symbol} — journal aligned to the broker"
                            ),
                        )
                    )
                continue
            actions.append(
                Action(
                    "alert_qty_mismatch", symbol=group[0].symbol, qty=pos.qty,
                    reason=(
                        f"QT's open trades total {ours:g} {group[0].symbol} but the broker holds "
                        f"{pos.qty:g}"
                        + (f" across {len(group)} strategies" if len(group) > 1 else "")
                        + " — not auto-corrected"
                    ),
                )
            )

    # Case (b): positions Alpaca holds that no DB trade accounts for.
    for norm_symbol, pos in pos_by_symbol.items():
        if norm_symbol not in db_symbols:
            actions.append(
                Action(
                    "alert_orphan_position", symbol=pos.symbol, qty=pos.qty,
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
            asset_class=t.asset_class or "stock",
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
            await notify.slack_cat(
                session,
                "reconciliation",
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

        elif action.kind == "adjust_qty_fee_in_kind" and trade is not None:
            # Quietly correct, and say so in the audit log — but NO Slack alert.
            # This fires on every crypto entry by construction, so alerting would
            # train the user to ignore the channel that also carries real drift.
            before = trade.qty
            trade.qty = action.qty
            trade.notional = (trade.entry_price or 0.0) * trade.qty
            session.add(AuditLog(
                category="reconcile",
                message=(
                    f"[{mode}] {trade.symbol} qty {before:g} -> {trade.qty:g} "
                    "(crypto fee paid in coin)"
                ),
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
            await notify.slack_cat(
                session,
                "reconciliation",
                f":warning: *{mode.upper()}* Alpaca holds {action.qty:g} {action.symbol} that QT "
                "has no open trade for. Not auto-adopting — check manually.",
            )

        elif action.kind == "alert_qty_mismatch":
            # Deliberately not self-healing. We know the totals disagree; we do
            # NOT know which strategy's row is wrong, and picking one would
            # either invent shares or write off real ones.
            session.add(AuditLog(
                category="reconcile",
                message=f"[{mode}] QUANTITY MISMATCH on {action.symbol}",
                detail=action.reason,
            ))
            await notify.slack_cat(
                session,
                "reconciliation",
                f":warning: *{mode.upper()}* {action.reason}. Not auto-corrected — check manually.",
            )

    if actions:
        log.info("reconciliation applied %d action(s) in %s mode", len(actions), mode)
    return actions
