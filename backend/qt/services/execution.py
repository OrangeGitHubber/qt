"""Trade execution: shadow (journal only) and paper (real Alpaca paper
orders, marketable limit only, idempotent client order IDs)."""

import asyncio
import logging
import uuid
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from qt.broker.alpaca import AlpacaClient, AlpacaError
from qt.models import AuditLog, Strategy, Trade
from qt.services import notify
from qt.services.engine import Candidate

log = logging.getLogger("qt.execution")

ENTRY_SLIP = 1.005  # buy limit 0.5% through the price = "marketable"
EXIT_SLIP = 0.99  # sell limit 1% through = exits must fill
FILL_POLL_SECONDS = (1, 2, 3)  # ~6s total


def _round_price(price: float) -> float:
    # US equities disallow sub-penny limits at >=$1; crypto is fine with more.
    return round(price, 2) if price >= 1 else round(price, 6)


def _qty_for(asset_class: str, sizing_usd: float, price: float) -> float:
    if asset_class == "stock":
        return float(int(sizing_usd // price))  # whole shares
    return round(sizing_usd / price, 6)


async def _await_fill(client: AlpacaClient, order_id: str) -> dict | None:
    for delay in FILL_POLL_SECONDS:
        await asyncio.sleep(delay)
        order = await client.get_order(order_id)
        if order.get("status") == "filled":
            return order
        if order.get("status") in ("canceled", "expired", "rejected"):
            return None
    return None


async def open_trade(
    session: Session,
    client: AlpacaClient,
    strategy: Strategy,
    version_id: int | None,
    mode: str,
    cand: Candidate,
    reason: str,
) -> Trade | None:
    qty = _qty_for(cand.asset_class, strategy.sizing_usd, cand.price)
    if qty <= 0:
        session.add(
            Trade(
                strategy_id=strategy.id, config_version_id=version_id, mode=mode,
                symbol=cand.symbol, asset_class=cand.asset_class, qty=0, notional=0,
                status="rejected",
                entry_reason=f"wanted to buy ({reason}) but position too small: "
                f"${strategy.sizing_usd:,.0f} buys 0 shares at ${cand.price:,.2f}",
            )
        )
        return None

    now = datetime.now(timezone.utc)
    trade = Trade(
        strategy_id=strategy.id, config_version_id=version_id, mode=mode,
        symbol=cand.symbol, asset_class=cand.asset_class, qty=qty,
        notional=qty * cand.price, status="open", entry_reason=reason,
        entry_price=cand.price, entry_at=now, high_water=cand.price,
    )

    if mode == "paper":
        client_order_id = f"qt-{uuid.uuid4().hex[:20]}"
        limit = _round_price(cand.price * ENTRY_SLIP)
        try:
            order = await client.submit_order(
                cand.symbol, qty, "buy", limit, client_order_id,
                time_in_force="gtc" if cand.asset_class == "crypto" else "day",
            )
        except AlpacaError as exc:
            trade.status = "rejected"
            trade.entry_reason = f"wanted to buy ({reason}) but order rejected: {exc}"
            session.add(trade)
            return None
        filled = await _await_fill(client, order["id"])
        if not filled:
            try:
                await client.cancel_order(order["id"])
            except AlpacaError:
                pass
            trade.status = "rejected"
            trade.entry_reason = f"wanted to buy ({reason}) but limit order did not fill"
            session.add(trade)
            return None
        trade.entry_order_id = order["id"]
        trade.entry_price = float(filled.get("filled_avg_price") or cand.price)
        trade.qty = float(filled.get("filled_qty") or qty)
        trade.notional = trade.entry_price * trade.qty
        trade.high_water = trade.entry_price

    session.add(trade)
    session.add(
        AuditLog(
            category="trade",
            message=f"[{mode}] BUY {trade.qty:g} {cand.symbol} @ ~${trade.entry_price:,.4f}",
            detail=reason,
        )
    )
    await notify.slack(
        session,
        f":large_green_circle: *{mode.upper()}* bought {trade.qty:g} {cand.symbol} "
        f"@ ${trade.entry_price:,.4f} — {reason} (strategy: {strategy.name})",
    )
    return trade


async def close_trade(
    session: Session, client: AlpacaClient, trade: Trade, price: float, reason: str
) -> bool:
    exit_price = price

    if trade.mode == "paper":
        client_order_id = f"qt-x-{uuid.uuid4().hex[:18]}"
        limit = _round_price(price * EXIT_SLIP)
        try:
            order = await client.submit_order(
                trade.symbol, trade.qty, "sell", limit, client_order_id,
                time_in_force="gtc" if trade.asset_class == "crypto" else "day",
            )
        except AlpacaError as exc:
            session.add(
                AuditLog(
                    category="trade",
                    message=f"[paper] SELL {trade.symbol} FAILED — will retry next cycle",
                    detail=str(exc),
                )
            )
            return False
        filled = await _await_fill(client, order["id"])
        if not filled:
            try:
                await client.cancel_order(order["id"])
            except AlpacaError:
                pass
            session.add(
                AuditLog(
                    category="trade",
                    message=f"[paper] SELL {trade.symbol} did not fill — will retry next cycle",
                )
            )
            return False
        trade.exit_order_id = order["id"]
        exit_price = float(filled.get("filled_avg_price") or price)

    trade.exit_price = exit_price
    trade.exit_at = datetime.now(timezone.utc)
    trade.exit_reason = reason
    trade.pnl = round((exit_price - (trade.entry_price or exit_price)) * trade.qty, 2)
    trade.status = "closed"

    pnl_pct = ((exit_price / trade.entry_price - 1) * 100) if trade.entry_price else 0
    emoji = ":chart_with_upwards_trend:" if trade.pnl >= 0 else ":chart_with_downwards_trend:"
    session.add(
        AuditLog(
            category="trade",
            message=f"[{trade.mode}] SELL {trade.qty:g} {trade.symbol} @ ${exit_price:,.4f} "
            f"→ P&L ${trade.pnl:,.2f} ({pnl_pct:+.2f}%)",
            detail=reason,
        )
    )
    await notify.slack(
        session,
        f"{emoji} *{trade.mode.upper()}* sold {trade.qty:g} {trade.symbol} @ ${exit_price:,.4f} — "
        f"{reason} → P&L *${trade.pnl:,.2f}* ({pnl_pct:+.2f}%)",
    )
    return True
