"""Engine control endpoints: mode ladder, risk rails, journal, scoreboard."""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import func
from sqlalchemy.orm import Session

from qt.api.deps import leverage_unlockable
from qt.broker.factory import get_client
from qt.db import get_session
from qt.models import AuditLog, Strategy, Trade
from qt.services import regime, scoreboard
from qt.services.engine import ENGINE_MODES, get_mode, get_risk
from qt.settings_service import get_setting, set_setting

router = APIRouter(prefix="/api/engine", tags=["engine"])

LEVERAGE_CONFIRM_PHRASE = "I ACCEPT AMPLIFIED LOSSES"


@router.get("")
async def engine_state(session: Session = Depends(get_session)) -> dict:
    mode = get_mode(session)
    risk = get_risk(session)
    unlockable = leverage_unlockable()
    if not unlockable:
        risk["leverage_enabled"] = False  # env lock always wins

    regime_info = None
    client = get_client(session)
    if client:
        try:
            regime_info = await regime.regime_status(client)
        except Exception:
            regime_info = {"ok": False, "detail": "regime check unavailable"}

    from datetime import datetime, timezone

    today = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    realized = (
        session.query(func.coalesce(func.sum(Trade.pnl), 0.0))
        .filter(Trade.mode == mode, Trade.status == "closed", Trade.exit_at >= today)
        .scalar()
    )
    open_count = (
        session.query(func.count(Trade.id))
        .filter(Trade.mode == mode, Trade.status == "open")
        .scalar()
    )
    entries_today = (
        session.query(func.count(Trade.id))
        .filter(Trade.mode == mode, Trade.entry_at >= today, Trade.status != "rejected")
        .scalar()
    )
    return {
        "mode": mode,
        "modes": list(ENGINE_MODES),
        "risk": risk,
        "regime": regime_info,
        "regime_filter_enabled": get_setting(session, "regime_filter_enabled") is not False,
        "leverage": {"unlockable": unlockable, "enabled": bool(risk.get("leverage_enabled"))},
        "slack_configured": bool(get_setting(session, "slack_webhook_url")),
        "today": {
            "realized_pnl": float(realized or 0),
            "open_positions": int(open_count or 0),
            "entries": int(entries_today or 0),
        },
    }


class ModeBody(BaseModel):
    mode: str
    confirm: bool = False


@router.post("/mode")
def set_mode(body: ModeBody, session: Session = Depends(get_session)) -> dict:
    if body.mode not in ENGINE_MODES:
        raise HTTPException(status_code=422, detail=f"Mode must be one of {ENGINE_MODES}.")
    if body.mode == "paper" and not body.confirm:
        raise HTTPException(
            status_code=428,
            detail="Paper mode places simulated orders on your Alpaca paper account. Confirm to proceed.",
        )
    if body.mode == "paper":
        enabled = session.query(func.count(Strategy.id)).filter(Strategy.enabled.is_(True)).scalar()
        if not enabled:
            raise HTTPException(status_code=409, detail="Enable at least one strategy first.")
    set_setting(session, "engine_mode", body.mode)
    session.add(AuditLog(category="engine", message=f"Engine mode set to {body.mode.upper()}"))
    return {"mode": body.mode}


class RiskBody(BaseModel):
    max_daily_loss_usd: float = Field(ge=10, le=1_000_000)
    max_daily_loss_pct: float = Field(ge=0.5, le=50)
    max_total_positions: int = Field(ge=1, le=50)
    max_total_exposure_usd: float = Field(ge=10, le=10_000_000)
    max_trades_per_day: int = Field(ge=1, le=200)
    cooldown_hours_after_loss: float = Field(ge=0, le=720)
    wash_sale_guard: str = Field(pattern="^(block|warn|off)$")
    leverage_enabled: bool = False
    leverage_confirm: str = ""


@router.put("/risk")
async def set_risk(body: RiskBody, session: Session = Depends(get_session)) -> dict:
    current = get_risk(session)
    payload = body.model_dump()
    confirm = payload.pop("leverage_confirm", "")

    if payload["leverage_enabled"]:
        if not leverage_unlockable():
            raise HTTPException(
                status_code=403,
                detail=(
                    "Leverage is locked at the server level. Set QT_ALLOW_LEVERAGE=true on the "
                    "Docker container to make this option available — see docs/how-it-works.md."
                ),
            )
        if not current.get("leverage_enabled") and confirm != LEVERAGE_CONFIRM_PHRASE:
            raise HTTPException(
                status_code=428,
                detail=f'Type exactly "{LEVERAGE_CONFIRM_PHRASE}" to enable leverage.',
            )

    if payload["leverage_enabled"] != bool(current.get("leverage_enabled")):
        state = "ENABLED" if payload["leverage_enabled"] else "disabled"
        session.add(AuditLog(category="risk", message=f"⚠ LEVERAGE {state}"))
        from qt.services import notify

        await notify.slack(session, f":warning: Leverage {state} in QT risk settings.")

    set_setting(session, "risk_config", payload)
    session.add(AuditLog(category="risk", message="Risk configuration updated", detail=str(payload)))
    return get_risk(session)


class RegimeBody(BaseModel):
    enabled: bool


@router.put("/regime")
def set_regime(body: RegimeBody, session: Session = Depends(get_session)) -> dict:
    set_setting(session, "regime_filter_enabled", body.enabled)
    session.add(
        AuditLog(category="risk", message=f"Regime filter {'enabled' if body.enabled else 'DISABLED'}")
    )
    return {"enabled": body.enabled}


class SlackBody(BaseModel):
    url: str = ""


@router.put("/slack")
def set_slack(body: SlackBody, session: Session = Depends(get_session)) -> dict:
    url = body.url.strip()
    if url and not url.startswith("https://hooks.slack.com/"):
        raise HTTPException(status_code=422, detail="That doesn't look like a Slack incoming-webhook URL.")
    set_setting(session, "slack_webhook_url", url or None)
    session.add(AuditLog(category="config", message=f"Slack webhook {'set' if url else 'cleared'}"))
    return {"configured": bool(url)}


@router.post("/slack/test")
async def test_slack(session: Session = Depends(get_session)) -> dict:
    from qt.services import notify

    ok = await notify.slack(session, ":wave: QT test notification — Slack is wired up.")
    if not ok:
        raise HTTPException(status_code=502, detail="Slack rejected the message (or no webhook is set).")
    return {"ok": True}


@router.get("/journal")
def journal(
    mode: str | None = None,
    limit: int = 100,
    session: Session = Depends(get_session),
) -> list[dict]:
    q = session.query(Trade, Strategy.name).join(Strategy, Trade.strategy_id == Strategy.id)
    if mode:
        q = q.filter(Trade.mode == mode)
    rows = q.order_by(Trade.id.desc()).limit(min(limit, 500)).all()
    return [
        {
            "id": t.id,
            "strategy": name,
            "mode": t.mode,
            "symbol": t.symbol,
            "asset_class": t.asset_class,
            "status": t.status,
            "qty": t.qty,
            "notional": t.notional,
            "entry_price": t.entry_price,
            "entry_at": t.entry_at.isoformat() if t.entry_at else None,
            "entry_reason": t.entry_reason,
            "exit_price": t.exit_price,
            "exit_at": t.exit_at.isoformat() if t.exit_at else None,
            "exit_reason": t.exit_reason,
            "pnl": t.pnl,
            "config_version_id": t.config_version_id,
        }
        for t, name in rows
    ]


@router.get("/scoreboard")
def get_scoreboard(session: Session = Depends(get_session)) -> dict:
    return scoreboard.series(session)
