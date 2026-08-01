"""Engine control endpoints: mode ladder, risk rails, journal, scoreboard."""

import logging
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
from qt.services.calendar import et_day
from qt.services.engine import ENGINE_MODES, get_mode, get_risk
from qt.settings_service import get_setting, set_setting

log = logging.getLogger("qt.api.engine")

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


@router.get("/positions")
async def open_positions(session: Session = Depends(get_session)) -> dict:
    """Every OPEN position across ALL strategies, with best-effort live prices —
    the account-wide answer to "which strategy holds the position that blocked
    my entry?" (the already-open rail is account-wide, so the holder is often a
    DIFFERENT strategy than the one that wanted in). Scoped to the current
    broker account, like the journal; shadow positions are included and
    labelled by their mode."""
    from qt.api.strategies import _snapshot_price

    trades = (
        session.query(Trade)
        .filter(Trade.status == "open", *_account_conditions(None, session))
        .order_by(Trade.entry_at)
        .all()
    )
    names = {s.id: s.name for s in session.query(Strategy).all()}
    rows = [
        {
            # The row's identity. Force-exit targets ONE trade, not a symbol —
            # another strategy may hold the same name and hasn't asked to be out.
            "trade_id": t.id,
            "strategy_id": t.strategy_id,
            "strategy_name": names.get(t.strategy_id, f"strategy #{t.strategy_id}"),
            "symbol": t.symbol,
            "asset_class": t.asset_class,
            "mode": t.mode,
            "qty": t.qty,
            "entry_price": t.entry_price,
            "notional": t.notional,
            "entry_at": t.entry_at.isoformat() if t.entry_at else None,
            "entry_reason": t.entry_reason,
            "current_price": None,
            "market_value": None,
            "unrealized_pnl": None,
            "unrealized_pct": None,
        }
        for t in trades
    ]

    # Live marks — best effort, one batched snapshot call per asset class.
    if rows:
        client = get_client(session)
        if client is not None:
            stock_syms = sorted({r["symbol"] for r in rows if r["asset_class"] == "stock"})
            crypto_syms = sorted({r["symbol"] for r in rows if r["asset_class"] == "crypto"})
            snaps: dict = {}
            try:
                if stock_syms:
                    snaps.update(await client.stock_snapshots(stock_syms))
                if crypto_syms:
                    snaps.update(await client.crypto_snapshots(crypto_syms))
            except Exception:  # noqa: BLE001 — never fail the view on a price hiccup
                snaps = {}
            for r in rows:
                price = _snapshot_price(snaps.get(r["symbol"]) or {})
                if price and r["entry_price"]:
                    r["current_price"] = price
                    r["market_value"] = round(price * r["qty"], 2)
                    r["unrealized_pnl"] = round((price - r["entry_price"]) * r["qty"], 2)
                    r["unrealized_pct"] = round((price / r["entry_price"] - 1) * 100, 2)

    total_cost = sum((r["notional"] or 0) for r in rows)
    total_value = sum((r["market_value"] or 0) for r in rows if r["market_value"] is not None)
    total_pnl = sum((r["unrealized_pnl"] or 0) for r in rows if r["unrealized_pnl"] is not None)
    return {
        "positions": rows,
        "total_cost": round(total_cost, 2),
        "total_value": round(total_value, 2),
        "total_unrealized_pnl": round(total_pnl, 2),
    }


@router.post("/positions/{trade_id}/close")
async def force_close_position(trade_id: int, session: Session = Depends(get_session)) -> dict:
    """Sell one open position NOW, at market, regardless of its strategy's rules.

    The manual override: you've decided to be out and don't want to wait for a
    stop to be touched. Deliberately a MARKET order — the escalating marketable
    limit the engine normally uses is there to protect the price on a routine
    exit, and it can sit unfilled. "Force exit" that might not fill would be a
    button that lies.

    Scoped to ONE trade, not the symbol: another strategy may hold the same name
    and has not asked to be closed.
    """
    trade = session.get(Trade, trade_id)
    if trade is None:
        raise HTTPException(status_code=404, detail="Position not found.")
    if trade.status != "open":
        raise HTTPException(status_code=409, detail=f"That position is already {trade.status}.")

    if trade.mode == "shadow":
        # Shadow never placed an order, so there is nothing at the broker to sell.
        # Close the journal row so the paper trail matches what you decided.
        trade.status = "closed"
        trade.exit_at = datetime.now(timezone.utc)
        trade.exit_price = trade.entry_price
        trade.exit_reason = "force-closed by hand (shadow — no order was ever placed)"
        session.add(AuditLog(category="engine", message=f"Force-closed shadow position {trade.symbol}"))
        return {"ok": True, "symbol": trade.symbol, "mode": trade.mode}

    client = get_client(session)
    if client is None:
        raise HTTPException(status_code=503, detail="Alpaca isn't configured — can't place the sell order.")

    from qt.api.strategies import _snapshot_price
    from qt.services import execution

    price = trade.entry_price or 0.0
    try:
        snaps = (
            await client.crypto_snapshots([trade.symbol])
            if trade.asset_class == "crypto"
            else await client.stock_snapshots([trade.symbol])
        )
        price = _snapshot_price(snaps.get(trade.symbol) or {}) or price
    except Exception:  # noqa: BLE001 — a stale mark must not block a manual exit
        log.warning("force close %s: quote unavailable, using entry price as the mark", trade.symbol)

    ok = await execution.close_trade(
        session, client, trade, price, "force-closed by hand (market order)", market=True
    )
    if not ok:
        raise HTTPException(status_code=502, detail="The broker rejected the sell order — nothing was closed.")
    session.add(AuditLog(category="engine", message=f"Force-closed {trade.symbol} at market"))
    from qt.services import notify

    await notify.slack_cat(
        session, "trades", f":warning: Force-closed {trade.symbol} at market (manual override)."
    )
    return {"ok": True, "symbol": trade.symbol, "mode": trade.mode}


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
async def get_scoreboard(
    account: str | None = None, session: Session = Depends(get_session)
) -> dict:
    """The honesty meter, scoped to ONE broker account (default: the current one).
    Equity across an account switch isn't comparable — normalising over it turned
    a balance change into a fake −80% "loss".

    The stored series is snapshotted hourly, so today's point can lag. Best-effort,
    we refresh it from the live account before serving, making the last point
    current as of this request rather than up to an hour old."""
    if account in (None, "", "current"):
        client = get_client(session)
        if client is not None:
            try:
                await scoreboard.record_snapshot(session, client)
                session.commit()
            except Exception:  # noqa: BLE001 — a stale last point beats a broken card
                session.rollback()
    return scoreboard.series(session, account=None if account in ("", "current") else account)


async def _unrealized_by_strategy(
    session: Session, mode: str, acct: list
) -> tuple[dict[int, float], dict[int, int]]:
    """Mark each strategy's OPEN positions to market.

    Returns ({strategy_id: unrealized $}, {strategy_id: positions we could NOT
    price}). The second half matters: a quote hiccup or a symbol the snapshot
    doesn't cover would otherwise silently shrink the total, and a number that is
    quietly missing a position reads exactly like one that isn't. The caller
    surfaces the count rather than pretending the sum is complete.
    """
    from qt.api.strategies import _snapshot_price

    trades = (
        session.query(Trade)
        .filter(Trade.status == "open", Trade.mode == mode, *acct)
        .all()
    )
    if not trades:
        return {}, {}
    unpriced: dict[int, int] = {}
    pnl: dict[int, float] = {}
    client = get_client(session)
    snaps: dict = {}
    if client is not None:
        stock_syms = sorted({t.symbol for t in trades if t.asset_class == "stock"})
        crypto_syms = sorted({t.symbol for t in trades if t.asset_class == "crypto"})
        try:
            if stock_syms:
                snaps.update(await client.stock_snapshots(stock_syms))
            if crypto_syms:
                snaps.update(await client.crypto_snapshots(crypto_syms))
        except Exception:  # noqa: BLE001 — a price hiccup must not break the table
            snaps = {}
    for t in trades:
        price = _snapshot_price(snaps.get(t.symbol) or {})
        if price and t.entry_price:
            pnl[t.strategy_id] = pnl.get(t.strategy_id, 0.0) + (price - t.entry_price) * t.qty
        else:
            unpriced[t.strategy_id] = unpriced.get(t.strategy_id, 0) + 1
    return {k: round(v, 2) for k, v in pnl.items()}, unpriced


@router.get("/strategy-pnl")
async def strategy_pnl(account: str | None = None, session: Session = Depends(get_session)) -> dict:
    """Per-strategy P&L in the active mode — the exact breakdown the aggregate
    scoreboard hides.

    REALIZED is locked in and sums to the account's realized P&L. UNREALIZED
    marks the open positions to live quotes, so it moves with the market and is
    only as good as this instant's prices — the two are reported separately and
    never added together for you, because one is money you have and the other is
    money you might. Scoped to the current account by default (pass account=all /
    a specific id / untagged)."""
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
    unreal, unpriced = await _unrealized_by_strategy(session, mode, acct)

    def row(sid, realized, trades, wins):
        held = int(open_by.pop(sid, 0))
        missing = unpriced.get(sid, 0)
        # Nothing held is genuinely $0.00 of unrealized. Something held that we
        # couldn't price is UNKNOWN — those are different facts and the UI shows
        # them differently ("$0.00" vs "—").
        if not held:
            unrealized: float | None = 0.0
        elif sid in unreal:
            unrealized = unreal[sid]
        else:
            unrealized = None
        return {
            "strategy_id": sid,
            "name": names.get(sid, f"#{sid} (deleted)"),
            "realized_pnl": round(float(realized or 0), 2),
            "trades": int(trades or 0),
            "wins": int(wins or 0),
            "win_rate": round(wins / trades, 4) if trades else None,
            "open_positions": held,
            "unrealized_pnl": unrealized,
            "unpriced_positions": missing,
        }

    strategies = [row(sid, r, t, w) for sid, r, t, w in rows]
    # Strategies holding open positions but with no closed trades yet.
    strategies += [row(sid, 0.0, 0, 0) for sid in list(open_by)]
    strategies.sort(key=lambda s: s["realized_pnl"], reverse=True)
    realized_total = round(sum(s["realized_pnl"] for s in strategies), 2)
    unrealized_total = round(sum(v for v in unreal.values()), 2)
    return {
        "mode": mode,
        "realized_total": realized_total,
        "unrealized_total": unrealized_total,
        # How many open positions carry no live mark, account-wide. Non-zero means
        # unrealized_total is a floor, not the whole picture — said so in the UI.
        "unpriced_positions": sum(unpriced.values()),
        "strategies": strategies,
    }


@router.get("/strategy-pnl-daily")
def strategy_pnl_daily(days: int = 30, account: str | None = None, session: Session = Depends(get_session)) -> dict:
    """Per-strategy realized P&L bucketed by day (active mode) for a stacked
    daily-contribution chart. Derived from closed trades grouped by exit DATE ×
    strategy — no snapshot storage needed. Realized only; a day with no closed
    trades simply doesn't appear. `days` is the lookback window; days <= 0 means
    all time (no cutoff). Scoped to the current account by default."""
    mode = get_mode(session)
    filters = [Trade.mode == mode, Trade.status == "closed", Trade.exit_at.isnot(None)]
    filters += _account_conditions(account, session)
    if days > 0:
        # One extra day of slack: the SQL filter is on the raw UTC timestamp, and
        # the ET day it belongs to can be the one before. Trimmed exactly below.
        filters.append(Trade.exit_at >= datetime.now(timezone.utc) - timedelta(days=days + 1))
    # Grouped in PYTHON, not SQL. SQLite has no timezone database, so bucketing by
    # ET in SQL would mean a hardcoded offset that is wrong for half the year;
    # et_day() gets DST right. The row count here is a window of closed trades —
    # small enough that doing it in Python costs nothing.
    raw = session.query(Trade.exit_at, Trade.strategy_id, Trade.pnl).filter(*filters).all()
    cutoff_day = et_day(datetime.now(timezone.utc) - timedelta(days=days)) if days > 0 else ""
    totals: dict[tuple[str, int], float] = {}
    for exit_at, sid, pnl in raw:
        day = et_day(exit_at)
        if day < cutoff_day:
            continue
        totals[(day, sid)] = totals.get((day, sid), 0.0) + float(pnl or 0)

    names = dict(session.query(Strategy.id, Strategy.name).all())
    days_sorted = sorted({d for d, _ in totals})
    idx = {d: i for i, d in enumerate(days_sorted)}
    per_strategy: dict[int, list[float]] = {}
    for (d, sid), pnl in totals.items():
        vals = per_strategy.setdefault(sid, [0.0] * len(days_sorted))
        vals[idx[d]] = round(pnl, 2)
    strategies = [
        {"strategy_id": sid, "name": names.get(sid, f"#{sid} (deleted)"),
         "values": vals, "total": round(sum(vals), 2)}
        for sid, vals in per_strategy.items()
    ]
    strategies.sort(key=lambda s: s["total"], reverse=True)
    return {"mode": mode, "days": days_sorted, "strategies": strategies}
