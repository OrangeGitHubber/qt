from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from qt import __version__, security
from qt.broker.alpaca import (
    SECRET_KEY_ID,
    SECRET_KEY_SECRET,
    AlpacaClient,
    AlpacaError,
)
from qt.db import get_session
from qt.settings_service import get_setting

router = APIRouter(prefix="/api", tags=["status"])


@router.get("/health")
def health() -> dict:
    return {"ok": True, "version": __version__}


@router.get("/status")
async def status(session: Session = Depends(get_session)) -> dict:
    result: dict = {
        "version": __version__,
        "trading_mode": get_setting(session, "trading_mode"),
        "alpaca_configured": False,
        "broker": None,
        "market": None,
        "error": None,
    }
    key_id = security.get_secret(session, SECRET_KEY_ID)
    key_secret = security.get_secret(session, SECRET_KEY_SECRET)
    if not key_id or not key_secret:
        return result

    result["alpaca_configured"] = True
    client = AlpacaClient(key_id=key_id, key_secret=key_secret)
    try:
        account = await client.account()
        clock = await client.clock()
    except AlpacaError as exc:
        result["error"] = f"Alpaca API error {exc.status_code}: {exc}"
        return result
    except Exception:
        result["error"] = "Could not reach Alpaca (network problem?)"
        return result

    result["broker"] = {
        "account_number": account.get("account_number"),
        "status": account.get("status"),
        "equity": account.get("equity"),
        "cash": account.get("cash"),
        "buying_power": account.get("buying_power"),
        "currency": account.get("currency"),
    }
    result["market"] = {
        "is_open": clock.get("is_open"),
        "next_open": clock.get("next_open"),
        "next_close": clock.get("next_close"),
        "timestamp": clock.get("timestamp"),
    }
    return result
