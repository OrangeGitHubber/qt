"""Manual broker actions the user triggers from Settings.

Right now this is the "flatten the whole account" liquidation — close every
position the broker holds (including any the engine never tracked, i.e. orphans)
and reconcile the engine's open trades to closed. The intended use is starting
fresh: liquidate, then point QT at a different paper/live account via the setup
endpoint. Deliberately gated behind a typed confirmation in the UI.
"""

import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from qt.api.market import require_client
from qt.broker.alpaca import AlpacaClient, AlpacaError
from qt.db import get_session
from qt.models import AuditLog, Trade
from qt.services import notify

log = logging.getLogger("qt.api.broker")

router = APIRouter(prefix="/api/broker", tags=["broker"])


@router.post("/liquidate")
async def liquidate(
    session: Session = Depends(get_session),
    client: AlpacaClient = Depends(require_client),
) -> dict:
    """Flatten EVERY position at the broker (at market) and mark the engine's
    open trades closed. Irreversible — the UI requires a typed confirmation."""
    # Snapshot positions first, so we can record a reasonable exit price for the
    # engine's own trades and report which broker holdings QT never tracked.
    try:
        positions = await client.list_positions()
    except AlpacaError as exc:
        raise HTTPException(status_code=502, detail=f"Could not read positions ({exc.status_code}): {exc}")

    price_by_symbol: dict[str, float] = {}
    for p in positions:
        try:
            price_by_symbol[p.get("symbol")] = float(p.get("current_price") or p.get("avg_entry_price") or 0)
        except (TypeError, ValueError):
            pass

    try:
        results = await client.close_all_positions(cancel_orders=True)
    except AlpacaError as exc:
        raise HTTPException(status_code=502, detail=f"Liquidation failed ({exc.status_code}): {exc}")

    # Reconcile the engine's real (paper/live) open trades to closed. Shadow
    # trades are hypothetical — no broker position backs them — so leave them be.
    now = datetime.now(timezone.utc)
    open_trades = (
        session.query(Trade)
        .filter(Trade.status == "open", Trade.mode != "shadow")
        .all()
    )
    tracked = {t.symbol for t in open_trades}
    for t in open_trades:
        # Fall back to the entry price (zero P&L) rather than inventing a number.
        exit_price = price_by_symbol.get(t.symbol) or t.entry_price or 0.0
        t.status = "closed"
        t.exit_at = now
        t.exit_price = exit_price
        t.exit_reason = "manual liquidation (account reset)"
        if t.entry_price and t.qty:
            t.pnl = round((exit_price - t.entry_price) * t.qty, 2)

    closed = len(results) if isinstance(results, list) else 0
    broker_symbols = {r.get("symbol") for r in results if isinstance(r, dict)} if isinstance(results, list) else set()
    orphans = sorted(s for s in (broker_symbols - tracked) if s)

    msg = f"Manual liquidation: closed {closed} broker position(s), reconciled {len(open_trades)} engine trade(s)."
    session.add(AuditLog(category="broker", message=msg, detail=f"orphans_cleared={orphans}"))
    slack_text = f":rotating_light: {msg}" + (f" Orphans cleared: {', '.join(orphans)}." if orphans else "")
    await notify.slack_cat(session, "reconciliation", slack_text)

    return {
        "ok": True,
        "positions_closed": closed,
        "trades_reconciled": len(open_trades),
        "orphans_cleared": orphans,
    }
