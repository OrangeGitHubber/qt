"""Manual broker actions the user triggers from Settings.

Right now this is the "flatten the whole account" liquidation — close every
position the broker holds (including any the engine never tracked, i.e. orphans)
and reconcile the engine's open trades to closed. The intended use is starting
fresh: liquidate, then point QT at a different paper/live account via the setup
endpoint. Deliberately gated behind a typed confirmation in the UI.
"""

import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Body, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from qt.api.market import require_client
from qt.broker.factory import get_client
from qt.broker.alpaca import AlpacaClient, AlpacaError
from qt.db import get_session
from qt.models import AuditLog, Trade

log = logging.getLogger("qt.api.broker")

router = APIRouter(prefix="/api/broker", tags=["broker"])


class LiquidateBody(BaseModel):
    # Off by default: QT closes only the positions IT tracks, leaving anything
    # else alone — those "orphans" may belong to ANOTHER bot on the same Alpaca
    # account, and QT must not flatten someone else's trades. Turn it on only for
    # a true whole-account wipe.
    include_orphans: bool = False


def _norm(symbol: str) -> str:
    """Match QT's symbols to the broker's: QT stores crypto as 'AVAX/USD', Alpaca
    positions return it slash-less ('AVAXUSD'). Same rule reconciliation uses."""
    return symbol.replace("/", "").upper()


async def _liquidate_one_book(
    session: Session,
    client: AlpacaClient,
    mode: str,
    body: "LiquidateBody",
    now: datetime,
) -> dict:
    """Flatten ONE mode's book, through THAT mode's own broker client.

    Split out of the endpoint because the original selected every non-shadow
    trade and closed them all through a single (paper) client. Once live exists
    that is the worst shape of bug in this file: the paper account does not hold
    the live positions, so every close would error — and the loop below marks
    trades closed regardless of whether the close succeeded. QT would record a
    flat book while the live positions were still open, with the stops now
    unwatched because the trades are closed.
    """
    try:
        raw = await client.list_positions()
    except AlpacaError as exc:
        raise HTTPException(status_code=502, detail=f"Could not read positions ({exc.status_code}): {exc}")

    # Index broker positions by the normalized symbol.
    pos_by_norm: dict[str, dict] = {}
    for p in raw:
        sym = p.get("symbol") or ""
        try:
            qty = float(p.get("qty") or 0)
            price = float(p.get("current_price") or p.get("avg_entry_price") or 0)
        except (TypeError, ValueError):
            qty, price = 0.0, 0.0
        if sym and qty:
            pos_by_norm[_norm(sym)] = {"symbol": sym, "qty": qty, "price": price}

    # THIS MODE'S trades only. `!= "shadow"` was right when paper was the only
    # other mode and became a cross-account bug the moment live appeared.
    open_trades = (
        session.query(Trade)
        .filter(Trade.status == "open", Trade.mode == mode)
        .all()
    )
    tracked_norms = {_norm(t.symbol) for t in open_trades}
    errors: list[str] = []
    positions_closed = 0

    if body.include_orphans:
        try:
            results = await client.close_all_positions(cancel_orders=True)
        except AlpacaError as exc:
            raise HTTPException(status_code=502, detail=f"Liquidation failed ({exc.status_code}): {exc}")
        positions_closed = len(results) if isinstance(results, list) else 0
        orphans_cleared = sorted(p["symbol"] for n, p in pos_by_norm.items() if n not in tracked_norms)
        orphans_left: list[str] = []
    else:
        # Close only what QT holds, by its own quantity (never more than the
        # broker shows for that symbol — the rest could be another bot's).
        for t in open_trades:
            pos = pos_by_norm.get(_norm(t.symbol))
            if pos is None:
                continue  # broker isn't holding it — nothing to sell, just reconcile below
            qty_to_close = min(t.qty, pos["qty"]) if pos["qty"] else t.qty
            try:
                await client.close_position(pos["symbol"], qty=qty_to_close)
                positions_closed += 1
            except AlpacaError as exc:
                errors.append(f"{t.symbol}: {exc}")
        orphans_cleared = []
        orphans_left = sorted(p["symbol"] for n, p in pos_by_norm.items() if n not in tracked_norms)

    # Reconcile QT's open trades to closed (both modes) at the last known price;
    # fall back to entry price (zero P&L) rather than inventing a number.
    for t in open_trades:
        pos = pos_by_norm.get(_norm(t.symbol))
        exit_price = (pos["price"] if pos else 0.0) or t.entry_price or 0.0
        t.status = "closed"
        t.exit_at = now
        t.exit_price = exit_price
        t.exit_reason = "manual liquidation (account reset)"
        if t.entry_price and t.qty:
            t.pnl = round((exit_price - t.entry_price) * t.qty, 2)

    return {
        "book": mode,
        "positions_closed": positions_closed,
        "trades_reconciled": len(open_trades),
        "orphans_cleared": orphans_cleared,
        "orphans_left": orphans_left,
        "errors": [f"[{mode}] {e}" for e in errors],
    }


@router.post("/liquidate")
async def liquidate(
    body: LiquidateBody = Body(default=LiquidateBody()),
    session: Session = Depends(get_session),
    client: AlpacaClient = Depends(require_client),
) -> dict:
    """Flatten holdings and mark the engine's open trades closed. By default this
    closes ONLY the positions QT tracks (by exact quantity, so a co-existing bot's
    shares are untouched); set include_orphans to also flatten positions QT
    doesn't track. Irreversible — the UI requires a typed confirmation.

    EVERY ORDER-PLACING BOOK, each through its own client. This is the panic
    button, so "flatten everything" has to mean everything — a version that
    silently skipped the live account would be worse than one that did nothing,
    because it would report success. A book with no credentials stored has no
    positions to flatten and is skipped.

    Shadow is untouched: those trades are hypothetical and correspond to no
    broker position.
    """
    now = datetime.now(timezone.utc)
    books: list[dict] = []
    for mode in ("paper", "live"):
        mode_client = client if mode == "paper" else get_client(session, mode)
        if mode_client is None:
            continue
        books.append(await _liquidate_one_book(session, mode_client, mode, body, now))

    positions_closed = sum(b["positions_closed"] for b in books)
    trades_reconciled = sum(b["trades_reconciled"] for b in books)
    orphans_cleared = sorted({s for b in books for s in b["orphans_cleared"]})
    orphans_left = sorted({s for b in books for s in b["orphans_left"]})
    errors = [e for b in books for e in b["errors"]]

    scope = "whole account" if body.include_orphans else "QT holdings only"
    touched = ", ".join(b["book"] for b in books) or "none"
    msg = (
        f"Manual liquidation ({scope}, books: {touched}): closed {positions_closed} "
        f"position(s), reconciled {trades_reconciled} engine trade(s)."
    )
    detail = f"orphans_cleared={orphans_cleared} orphans_left={orphans_left} errors={errors}"
    session.add(AuditLog(category="broker", message=msg, detail=detail))
    extra = ""
    if orphans_cleared:
        extra = f" Orphans cleared: {', '.join(orphans_cleared)}."
    elif orphans_left:
        extra = f" Left {len(orphans_left)} untracked position(s) alone: {', '.join(orphans_left)}."
    from qt.services import notify

    await notify.slack_cat(session, "reconciliation", f":rotating_light: {msg}{extra}")

    return {
        "ok": True,
        "mode": "full" if body.include_orphans else "qt_only",
        "books": [b["book"] for b in books],
        "positions_closed": positions_closed,
        "trades_reconciled": trades_reconciled,
        "orphans_cleared": orphans_cleared,
        "orphans_left": orphans_left,
        "errors": errors,
    }
