from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from qt import __version__
from qt.api.deps import leverage_unlockable, require_user
from qt.broker.alpaca import AlpacaError
from qt.broker.factory import get_client
from qt.db import get_session
from qt.services import persistence
from qt.settings_service import get_setting

router = APIRouter(prefix="/api", tags=["status"])


@router.get("/health")
def health() -> dict:
    return {"ok": True, "version": __version__}


@router.get("/status")
async def status(
    session: Session = Depends(get_session), _user: str = Depends(require_user)
) -> dict:
    boot = persistence.boot_state()
    result: dict = {
        "version": __version__,
        "trading_mode": get_setting(session, "trading_mode"),
        "alpaca_configured": False,
        "leverage_unlockable": leverage_unlockable(),
        "data_persistent": boot.get("data_persistent"),
        "data_persistent_reason": boot.get("data_persistent_reason", ""),
        "secrets_without_key": boot.get("secrets_without_key", False),
        "instance_key_created_at": boot.get("instance_key_created_at"),
        "broker": None,
        "market": None,
        "error": None,
    }
    client = get_client(session)
    if client is None:
        return result

    result["alpaca_configured"] = True
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
