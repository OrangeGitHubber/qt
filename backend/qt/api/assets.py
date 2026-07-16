"""Symbol directory endpoints powering autocomplete."""

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from qt.api.market import require_client
from qt.broker.alpaca import AlpacaClient, AlpacaError
from qt.db import get_session
from qt.models import AuditLog
from qt.services import assets

router = APIRouter(prefix="/api/assets", tags=["assets"])


@router.get("/search")
def search(
    q: str = Query(default="", max_length=64),
    asset_class: str | None = Query(default=None, pattern="^(stock|crypto)$"),
    limit: int = Query(default=20, ge=1, le=50),
    session: Session = Depends(get_session),
) -> list[dict]:
    return assets.search(session, q, asset_class, limit)


@router.get("/status")
def directory_status(session: Session = Depends(get_session)) -> dict:
    return assets.status(session)


@router.post("/sync")
async def sync_now(
    session: Session = Depends(get_session), client: AlpacaClient = Depends(require_client)
) -> dict:
    try:
        counts = await assets.sync(session, client)
    except AlpacaError as exc:
        raise HTTPException(status_code=502, detail=f"Asset sync failed ({exc.status_code}): {exc}")
    session.add(AuditLog(category="config", message=f"Asset directory synced: {counts}"))
    return assets.status(session)
