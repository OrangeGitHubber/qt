"""First-run setup wizard endpoints: store Alpaca paper keys after
validating them against the live paper API."""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from qt import security
from qt.broker.alpaca import (
    SECRET_KEY_ID,
    SECRET_KEY_SECRET,
    AlpacaClient,
    AlpacaError,
)
from qt.db import get_session
from qt.models import AuditLog

router = APIRouter(prefix="/api/setup", tags=["setup"])


class AlpacaKeys(BaseModel):
    key_id: str = Field(min_length=1)
    key_secret: str = Field(min_length=1)


@router.get("/state")
def setup_state(session: Session = Depends(get_session)) -> dict:
    has_keys = security.get_secret(session, SECRET_KEY_ID) is not None
    return {"alpaca_configured": has_keys}


@router.post("/alpaca")
async def save_alpaca_keys(keys: AlpacaKeys, session: Session = Depends(get_session)) -> dict:
    client = AlpacaClient(key_id=keys.key_id, key_secret=keys.key_secret)
    try:
        account = await client.account()
    except AlpacaError as exc:
        raise HTTPException(
            status_code=400,
            detail=f"Alpaca rejected these keys ({exc.status_code}): {exc}",
        )
    except Exception:
        raise HTTPException(status_code=502, detail="Could not reach Alpaca. Check server internet access.")

    security.set_secret(session, SECRET_KEY_ID, keys.key_id)
    security.set_secret(session, SECRET_KEY_SECRET, keys.key_secret)
    session.add(AuditLog(category="setup", message="Alpaca paper keys saved and verified"))
    return {
        "ok": True,
        "account_number": account.get("account_number"),
        "status": account.get("status"),
    }
