"""First-run setup wizard endpoints: store Alpaca paper keys after
validating them against the live paper API."""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from qt import security
from qt.broker.alpaca import (
    LIVE_BASE_URL,
    LIVE_SECRET_KEY_ID,
    LIVE_SECRET_KEY_SECRET,
    SECRET_KEY_ID,
    SECRET_KEY_SECRET,
    AlpacaClient,
    AlpacaError,
)
from qt.broker.factory import live_credentials_stored
from qt.db import get_session
from qt.models import AuditLog

router = APIRouter(prefix="/api/setup", tags=["setup"])


class AlpacaKeys(BaseModel):
    key_id: str = Field(min_length=1)
    key_secret: str = Field(min_length=1)


@router.get("/state")
def setup_state(session: Session = Depends(get_session)) -> dict:
    has_keys = security.get_secret(session, SECRET_KEY_ID) is not None
    return {
        "alpaca_configured": has_keys,
        # Reported separately and never conflated: paper keys being present says
        # nothing about whether real money can move.
        "alpaca_live_configured": live_credentials_stored(session),
    }


@router.post("/alpaca/live")
async def save_alpaca_live_keys(
    keys: AlpacaKeys, session: Session = Depends(get_session)
) -> dict:
    """Store the LIVE Alpaca keys, after proving they are live keys.

    A SECOND pair, alongside the paper ones rather than replacing them: the point
    of per-strategy mode is that both books run at once, so both sets have to
    exist at once.

    VALIDATED AGAINST THE LIVE HOST, which is the check that matters. Alpaca
    issues different credentials for the two endpoints, so pasting the paper pair
    in here is rejected by Alpaca itself rather than by a guess of ours about key
    formats. Without that round trip, paper keys stored as live would let a
    strategy be promoted to live and then fail every order at the broker — the
    worst outcome, because the UI would say live and nothing would be trading.

    Storing keys does NOT start live trading. Three further deliberate acts are
    required: the strategy's own mode, the master switch, and a confirmation on
    each. This only makes the option exist.
    """
    client = AlpacaClient(
        key_id=keys.key_id, key_secret=keys.key_secret, base_url=LIVE_BASE_URL
    )
    try:
        account = await client.account()
    except AlpacaError as exc:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Alpaca's LIVE endpoint rejected these keys ({exc.status_code}): {exc}. "
                "Live and paper keys are different — paper keys will not work here."
            ),
        )
    except Exception:
        raise HTTPException(
            status_code=502, detail="Could not reach Alpaca. Check server internet access."
        )

    security.set_secret(session, LIVE_SECRET_KEY_ID, keys.key_id)
    security.set_secret(session, LIVE_SECRET_KEY_SECRET, keys.key_secret)
    # NOT written to `current_account_id`. That setting tags new trades, and the
    # paper engine is running right now — repointing it here would stamp paper
    # trades with the live account number.
    session.add(AuditLog(
        category="setup",
        message="Alpaca LIVE keys saved and verified against the live endpoint",
        detail=(
            "Stored only. No strategy trades live until its own mode is set to live "
            "AND the master engine mode is set to live, each with a confirmation."
        ),
    ))
    return {
        "ok": True,
        "account_number": account.get("account_number"),
        "status": account.get("status"),
    }


@router.delete("/alpaca/live")
def forget_alpaca_live_keys(session: Session = Depends(get_session)) -> dict:
    """Remove the live credentials — the fastest way to make live unreachable
    again, and it must never be harder than adding them. Strategies keep their
    `live` mode setting; with no keys `get_client` returns None and the live pass
    simply does not trade."""
    security.delete_secret(session, LIVE_SECRET_KEY_ID)
    security.delete_secret(session, LIVE_SECRET_KEY_SECRET)
    session.add(AuditLog(
        category="setup", message="Alpaca LIVE keys removed — live trading is unreachable"
    ))
    return {"ok": True, "alpaca_live_configured": False}


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
    # Remember which account these keys belong to so new trades get stamped with
    # it — that's what lets the journal / P&L views separate accounts after a
    # key switch.
    from qt.settings_service import set_setting

    if account.get("account_number"):
        set_setting(session, "current_account_id", account.get("account_number"))
    session.add(AuditLog(category="setup", message="Alpaca paper keys saved and verified"))
    return {
        "ok": True,
        "account_number": account.get("account_number"),
        "status": account.get("status"),
    }
