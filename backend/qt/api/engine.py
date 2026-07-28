"""Engine control endpoints: mode ladder, risk rails, journal, scoreboard."""

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import case, func
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


def _account_conditions(account: str | None, session: Session) -> list:
    """SQLAlchemy filter conditions to scope trades to one broker account. Default
    (account is None) = the CURRENT account, so the journal / P&L views auto-scope
    to it after a key switch. 'all' = no filter; 'untagged' = the legacy null
    trades (from before per-account tagging)."""
    if account == "all":
        return []
    if account == "untagged":
        return [Trade.account_id.is_(None)]
    if account is None:
        account = get_setting(session, "current_account_id")
    return [Trade.account_id == account] if account else []


@router.get("/accounts")
def accounts(session: Session = Depends(get_session)) -> dict:
    """Broker accounts present in the trade history, for the journal / P&L account
    selector. `current` is the one new trades are tagged with."""
    current = get_setting(session, "current_account_id")
    rows = session.query(Trade.account_id, func.count(Trade.id)).group_by(Trade.account_id).all()
    out = [
        {"id": acct, "trades": int(n), "is_current": acct == current, "untagged": acct is None}
        for acct, n in rows
    ]
    out.sort(key=lambda a: (not a["is_current"], a["untagged"], -a["trades"]))
    return {"current": current, "accounts": out}


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

        await notify.slack_cat(session, "risk_changes", f":warning: Leverage {state} in QT risk settings.")

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


@router.get("/slack/prefs")
def get_slack_prefs(session: Session = Depends(get_session)) -> dict:
    """The catalog of opt-in Slack message categories with each one's current
    on/off state. The UI renders directly from this, so labels/descriptions live
    in one place (the backend catalog)."""
    from qt.services import notify

    prefs = notify.notify_prefs(session)
    return {
        "configured": bool(get_setting(session, "slack_webhook_url")),
        "categories": [
            {"key": c["key"], "label": c["label"], "description": c["description"],
             "enabled": prefs[c["key"]]}
            for c in notify.NOTIFY_CATEGORIES
        ],
    }


class SlackPrefsBody(BaseModel):
    prefs: dict[str, bool]


@router.put("/slack/prefs")
def put_slack_prefs(body: SlackPrefsBody, session: Session = Depends(get_session)) -> dict:
    """Persist a (partial) set of category toggles; unknown keys are ignored."""
    from qt.services import notify

    return {"prefs": notify.set_notify_prefs(session, body.prefs)}


def _iso_utc(dt: datetime | None) -> str | None:
    """Emit an ISO timestamp that carries a UTC offset. Datetimes stored via
    SQLite come back NAIVE (SQLite drops tzinfo), and a browser parses an
    offset-less ISO string as LOCAL time — so without this the journal shows
    the UTC wall-clock mislabeled as local. Stamp UTC so the client converts."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat()


@router.get("/journal")
def journal(
    mode: str | None = None,
    status: str | None = None,
    asset_class: str | None = None,
    account: str | None = None,
    limit: int = 100,
    session: Session = Depends(get_session),
) -> list[dict]:
    q = session.query(Trade, Strategy.name).join(Strategy, Trade.strategy_id == Strategy.id)
    for cond in _account_conditions(account, session):
        q = q.filter(cond)
    if mode:
        q = q.filter(Trade.mode == mode)
    if asset_class in ("stock", "crypto"):
        q = q.filter(Trade.asset_class == asset_class)
    # Filter server-side so hiding the (often numerous) rejected rows doesn't
    # get eaten by the row limit — "trades" = actually-executed (open+closed).
    if status == "trades":
        q = q.filter(Trade.status.in_(("open", "closed")))
    elif status in ("open", "closed", "rejected"):
        q = q.filter(Trade.status == status)
    rows = q.order_by(Trade.id.desc()).limit(min(limit, 500)).all()
    return [
        {
            "id": t.id,
            "strategy": name,
            "mode": t.mode,
            "symbol": t.symbol,
            "asset_class": t.asset_class,
            "status": t.status,
            "logged_at": _iso_utc(t.created_at),
            "qty": t.qty,
            "notional": t.notional,
            "entry_price": t.entry_price,
            "entry_at": _iso_utc(t.entry_at),
            "entry_reason": t.entry_reason,
            "exit_price": t.exit_price,
            "exit_at": _iso_utc(t.exit_at),
            "exit_reason": t.exit_reason,
            "pnl": t.pnl,
            "config_version_id": t.config_version_id,
        }
        for t, name in rows
    ]


@router.get("/scoreboard")
def get_scoreboard(session: Session = Depends(get_session)) -> dict:
    return scoreboard.series(session)


@router.get("/strategy-pnl")
def strategy_pnl(account: str | None = None, session: Session = Depends(get_session)) -> dict:
    """Per-strategy REALIZED P&L in the active mode — the exact, additive
    breakdown the aggregate scoreboard hides. Realized only (locked-in, sums to
    the account's realized P&L); open positions are shown as a count. Unrealized
    marks would need live quotes and aren't included here. Scoped to the current
    account by default (pass account=all / a specific id / untagged)."""
    mode = get_mode(session)
    acct = _account_conditions(account, session)
    win = func.sum(case((Trade.pnl > 0, 1), else_=0))
    rows = (
        session.query(Trade.strategy_id, func.coalesce(func.sum(Trade.pnl), 0.0),
                      func.count(Trade.id), func.coalesce(win, 0))
        .filter(Trade.mode == mode, Trade.status == "closed", *acct)
        .group_by(Trade.strategy_id)
        .all()
    )
    open_by = dict(
        session.query(Trade.strategy_id, func.count(Trade.id))
        .filter(Trade.mode == mode, Trade.status == "open", *acct)
        .group_by(Trade.strategy_id)
        .all()
    )
    names = dict(session.query(Strategy.id, Strategy.name).all())

    def row(sid, realized, trades, wins):
        return {
            "strategy_id": sid,
            "name": names.get(sid, f"#{sid} (deleted)"),
            "realized_pnl": round(float(realized or 0), 2),
            "trades": int(trades or 0),
            "wins": int(wins or 0),
            "win_rate": round(wins / trades, 4) if trades else None,
            "open_positions": int(open_by.pop(sid, 0)),
        }

    strategies = [row(sid, r, t, w) for sid, r, t, w in rows]
    # Strategies holding open positions but with no closed trades yet.
    strategies += [row(sid, 0.0, 0, 0) for sid in list(open_by)]
    strategies.sort(key=lambda s: s["realized_pnl"], reverse=True)
    realized_total = round(sum(s["realized_pnl"] for s in strategies), 2)
    return {"mode": mode, "realized_total": realized_total, "strategies": strategies}


@router.get("/strategy-pnl-daily")
def strategy_pnl_daily(days: int = 30, account: str | None = None, session: Session = Depends(get_session)) -> dict:
    """Per-strategy realized P&L bucketed by day (active mode) for a stacked
    daily-contribution chart. Derived from closed trades grouped by exit DATE ×
    strategy — no snapshot storage needed. Realized only; a day with no closed
    trades simply doesn't appear. `days` is the lookback window; days <= 0 means
    all time (no cutoff). Scoped to the current account by default."""
    mode = get_mode(session)
    day_col = func.date(Trade.exit_at)  # 'YYYY-MM-DD' of the (UTC) exit timestamp
    filters = [Trade.mode == mode, Trade.status == "closed", Trade.exit_at.isnot(None)]
    filters += _account_conditions(account, session)
    if days > 0:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")
        filters.append(day_col >= cutoff)
    rows = (
        session.query(day_col, Trade.strategy_id, func.coalesce(func.sum(Trade.pnl), 0.0))
        .filter(*filters)
        .group_by(day_col, Trade.strategy_id)
        .all()
    )
    names = dict(session.query(Strategy.id, Strategy.name).all())
    days_sorted = sorted({d for d, _, _ in rows})
    idx = {d: i for i, d in enumerate(days_sorted)}
    per_strategy: dict[int, list[float]] = {}
    for d, sid, pnl in rows:
        vals = per_strategy.setdefault(sid, [0.0] * len(days_sorted))
        vals[idx[d]] = round(float(pnl or 0), 2)
    strategies = [
        {"strategy_id": sid, "name": names.get(sid, f"#{sid} (deleted)"),
         "values": vals, "total": round(sum(vals), 2)}
        for sid, vals in per_strategy.items()
    ]
    strategies.sort(key=lambda s: s["total"], reverse=True)
    return {"mode": mode, "days": days_sorted, "strategies": strategies}
