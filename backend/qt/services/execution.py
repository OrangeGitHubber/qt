"""Trade execution: shadow (journal only) and paper (real Alpaca paper
orders, marketable limit only, idempotent client order IDs)."""

import asyncio
import json
import logging
import uuid
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from qt.broker.alpaca import AlpacaClient, AlpacaError
from qt.models import AuditLog, Strategy, Trade
from qt.services import notify
from qt.services.engine import Candidate

log = logging.getLogger("qt.execution")

ENTRY_SLIP_PCT = 0.5  # default: buy limit 0.5% through the price = "marketable"
EXIT_SLIP_PCT = 1.0  # default: sell limit 1% through = exits must fill
FILL_POLL_SECONDS = (1, 2, 3)  # ~6s total

# trade.id -> consecutive failed exit attempts, for the escalating exit chase.
# In-memory: resets on restart (escalation simply restarts from the base buffer).
_exit_attempts: dict[int, int] = {}


def _escalated_exit_pct(base: float, cap: float | None, attempts: int) -> float:
    """The exit marketable buffer for this attempt: start at `base`, widen by one
    base step per prior miss, capped at `cap`. cap<=base means no escalation."""
    base = max(0.0, base)
    cap = max(base, cap if cap is not None else base)
    return min(cap, base + attempts * base)


def _entry_slip_pct(strategy: Strategy) -> float:
    try:
        return float(json.loads(strategy.params).get("entry", {}).get("entry_slippage_pct", ENTRY_SLIP_PCT))
    except Exception:  # noqa: BLE001 — bad/old params: fall back to the default
        return ENTRY_SLIP_PCT


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
    sizing_usd: float | None = None,
) -> Trade | None:
    # `sizing_usd` lets the caller override the strategy's fixed $ per trade — the
    # engine passes the ATR-derived size when ATR sizing is on. None = the fixed
    # strategy.sizing_usd, so existing callers are unaffected.
    effective_sizing = strategy.sizing_usd if sizing_usd is None else sizing_usd
    qty = _qty_for(cand.asset_class, effective_sizing, cand.price)
    if qty <= 0:
        session.add(
            Trade(
                strategy_id=strategy.id, config_version_id=version_id, mode=mode,
                symbol=cand.symbol, asset_class=cand.asset_class, qty=0, notional=0,
                status="rejected",
                entry_reason=f"wanted to buy ({reason}) but position too small: "
                f"${effective_sizing:,.0f} buys 0 shares at ${cand.price:,.2f}",
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
        limit = _round_price(cand.price * (1 + _entry_slip_pct(strategy) / 100))
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
    await notify.slack_cat(
        session,
        "trade_confirmations",
        f":large_green_circle: *{mode.upper()}* bought {trade.qty:g} × *{cand.symbol}* "
        f"@ ${trade.entry_price:,.4f} · reason: {reason} · strategy: {strategy.name}",
    )
    return trade


async def close_trade(
    session: Session,
    client: AlpacaClient,
    trade: Trade,
    price: float,
    reason: str,
    *,
    slip_pct: float = EXIT_SLIP_PCT,
    slip_max_pct: float | None = None,
) -> bool:
    exit_price = price

    if trade.mode == "paper":
        client_order_id = f"qt-x-{uuid.uuid4().hex[:18]}"
        # Escalating marketable sell: widen the buffer with each prior miss (up to
        # slip_max_pct) so a fast drop still gets out — still a limit, never a
        # naked market order.
        attempts = _exit_attempts.get(trade.id, 0)
        pct = _escalated_exit_pct(slip_pct, slip_max_pct, attempts)
        limit = _round_price(price * (1 - pct / 100))
        try:
            order = await client.submit_order(
                trade.symbol, trade.qty, "sell", limit, client_order_id,
                time_in_force="gtc" if trade.asset_class == "crypto" else "day",
            )
        except AlpacaError as exc:
            _exit_attempts[trade.id] = attempts + 1
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
            _exit_attempts[trade.id] = attempts + 1
            session.add(
                AuditLog(
                    category="trade",
                    message=f"[paper] SELL {trade.symbol} did not fill at {pct:.1f}% "
                    f"(attempt {attempts + 1}) — will retry next cycle",
                )
            )
            return False
        _exit_attempts.pop(trade.id, None)  # filled — reset the escalation
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
    await notify.slack_cat(
        session,
        "trade_confirmations",
        f"{emoji} *{trade.mode.upper()}* sold {trade.qty:g} × *{trade.symbol}* @ ${exit_price:,.4f} "
        f"· reason: {reason} · P&L *${trade.pnl:,.2f}* ({pnl_pct:+.2f}%)",
    )
    return True
