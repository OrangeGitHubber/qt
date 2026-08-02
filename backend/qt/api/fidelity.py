"""Is the backtester telling the truth? Point it at a period you already traded.

A DEVELOPMENT AND VALIDATION TOOL, not part of the daily trading loop. Its job is
to earn (or withdraw) trust in the replay: run the same strategy over a window
you have real trades for, and diff the two. Once the replay is trusted this page
goes quiet and stays quiet — which is the point. It is the instrument, not the
machine.

Two endpoints:

  POST /api/fidelity/compare — replay a window this instance has traded, and diff.
  GET  /api/fidelity/export  — the journal for a window as plain JSON, so a
                               separate (production) instance can hand its real
                               trades to a development one for the same diff.

The export deliberately carries trades and nothing else: no keys, no account
numbers, no settings. It leaves one machine and lands on another, so it should be
boring to lose.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import func
from sqlalchemy.orm import Session

from qt.api.backtest import (
    BacktestBody,
    _mixed_resolution,
    _strategy_symbols,
    _uses_daily_only_signals,
    run,
)
from qt.api.market import require_client
from qt.broker.alpaca import AlpacaClient
from qt.db import get_session
from qt.api import baskets as baskets_api
from qt.models import BasketItem, Strategy, StrategyConfigVersion, Trade
from qt.services import fidelity
from qt.services.backtest import _day_fn

log = logging.getLogger("qt.api.fidelity")

router = APIRouter(prefix="/api/fidelity", tags=["fidelity"])


class CompareBody(BaseModel):
    strategy_id: int
    days: int = Field(default=90, ge=7, le=730)
    # Which real history to judge against. Paper is the honest default: it has
    # the volume, and DECISION fidelity is exactly as testable there as live.
    # Execution fidelity is not — Alpaca's paper fills are simulated, so
    # calibrating costs against them would bake in a fiction. See the note the
    # response carries.
    mode: str = Field(default="paper", pattern="^(paper|live|shadow)$")
    # Trades exported from another instance. When present these are compared
    # instead of this instance's own journal — the prod → dev path.
    imported_trades: list[dict] | None = None


def _aware(ts: datetime | None) -> datetime | None:
    """SQLite hands back naive datetimes even for timezone-aware columns, and
    comparing one against an aware `since` raises. Everything QT stores is UTC,
    so saying so is a restatement rather than an assumption."""
    if ts is None:
        return None
    return ts if ts.tzinfo is not None else ts.replace(tzinfo=timezone.utc)


def _day_of(asset_class: str):
    """The SAME day boundary the replay buckets by — ET for stocks, UTC for
    crypto — reused from the backtester rather than re-derived here. Matching is
    by day, so both sides must agree what a day IS; roll one of them and a trade
    near midnight lands in a different bucket and reads as a mismatch that never
    happened."""
    return _day_fn("crypto" if asset_class == "crypto" else "stock")


def _basket_drift(session: Session, strategy: Strategy, live_rows: list[dict]) -> tuple[list[dict], int]:
    """Whether the basket's MEMBERS changed, and how often, across these trades.

    Snapshots are timestamp-exact — an edit at 14:32 is recorded at 14:32, so a
    trade at 14:00 resolves to the old membership and one at 15:00 to the new.
    Nothing is rounded to a day.

    The anchor is the EARLIEST trade, not the latest. Comparing today against the
    most recent trade answers a narrower question than it appears to: a basket
    edited early in the window and left alone since would show no difference at
    all, while every trade before that edit ran on a different list.

    The count is the other half. Membership that changed and changed back reads
    as identical from any two points, so the number of snapshots taken during the
    window is the only thing that can say the universe moved underneath these
    trades. Returned separately because it is a different claim from "these two
    lists differ", not a stronger version of it.
    """
    if strategy.universe != "basket" or not strategy.basket_id:
        return [], 0
    from qt.models import BasketVersion

    earliest = (
        session.query(func.min(Trade.entry_at))
        .filter(Trade.strategy_id == strategy.id, Trade.entry_at.isnot(None))
        .scalar()
    )
    if earliest is None:
        return [], 0  # imported trades carry no timestamps — nothing to key on
    since = _aware(earliest)
    changes_during = (
        session.query(func.count(BasketVersion.id))
        .filter(
            BasketVersion.basket_id == strategy.basket_id,
            BasketVersion.created_at > since,
        )
        .scalar()
        or 0
    )
    then = baskets_api.members_at(session, strategy.basket_id, since)
    if then is None:
        return [], changes_during
    then_set = {(m["asset_class"], m["symbol"]) for m in then}
    now_set = {
        (i.asset_class, i.symbol)
        for i in session.query(BasketItem).filter(BasketItem.basket_id == strategy.basket_id)
    }
    if then_set == now_set:
        return [], changes_during
    added = sorted(s for _, s in now_set - then_set)
    removed = sorted(s for _, s in then_set - now_set)
    return (
        [
            {
                "field": "Basket members",
                "then": f"{len(then_set)} symbols"
                + (f" (since removed: {', '.join(removed)})" if removed else ""),
                "now": f"{len(now_set)} symbols"
                + (f" (since added: {', '.join(added)})" if added else ""),
            }
        ],
        changes_during,
    )


def _serialize_current(strategy: Strategy) -> dict:
    """Today's config in the same shape a version snapshot holds, so the two can
    be diffed field by field rather than eyeballed."""
    return {
        "universe": strategy.universe,
        "symbols": json.loads(strategy.symbols) if strategy.symbols else [],
        "basket_id": strategy.basket_id,
        "rank_by": strategy.rank_by,
        "top_n": strategy.top_n,
        "rank_enabled": bool(strategy.rank_enabled),
        "sizing_usd": strategy.sizing_usd,
        "sleeve_usd": strategy.sleeve_usd,
        "max_positions": strategy.max_positions,
        "swing_mode": strategy.swing_mode,
        "ignore_regime": strategy.ignore_regime,
        "params": json.loads(strategy.params),
    }


def _journal_rows(session: Session, strategy: Strategy, days: int, mode: str) -> list[dict]:
    """This instance's own trades for the window, shaped for the comparison.

    REJECTED rows are included on purpose. They are the only way to tell a trade
    the backtest invented from one the engine wanted and a rail refused, and
    those two mean opposite things about the replay."""
    since = datetime.now(timezone.utc) - timedelta(days=days)
    day_of = _day_of(strategy.asset_class)
    rows = (
        session.query(Trade)
        .filter(Trade.strategy_id == strategy.id, Trade.mode == mode)
        .all()
    )
    out: list[dict] = []
    for t in rows:
        # A rejected row never filled, so it has no entry_at — fall back to when
        # it was written, or the window would silently drop exactly the rows that
        # tell a blocked trade from an invented one.
        stamp = _aware(t.entry_at) or _aware(t.created_at)
        if stamp is None or stamp < since:
            continue
        out.append(
            {
                "symbol": t.symbol,
                "status": t.status,
                "entry_day": day_of(t.entry_at) if t.entry_at else None,
                "exit_day": day_of(t.exit_at) if t.exit_at else None,
                "entry_price": t.entry_price,
                "exit_price": t.exit_price,
                "pnl": t.pnl,
                "entry_reason": t.entry_reason,
                "exit_reason": t.exit_reason,
                "config_version_id": t.config_version_id,
            }
        )
    return out


@router.post("/compare")
async def compare(
    body: CompareBody,
    session: Session = Depends(get_session),
    client: AlpacaClient = Depends(require_client),
) -> dict:
    """Replay the window and diff it against what really happened."""
    strategy = session.get(Strategy, body.strategy_id)
    if not strategy:
        raise HTTPException(status_code=404, detail="Strategy not found.")

    live_rows = (
        body.imported_trades
        if body.imported_trades is not None
        else _journal_rows(session, strategy, body.days, body.mode)
    )
    if not live_rows:
        raise HTTPException(
            status_code=422,
            detail=f"No {body.mode} trades for \"{strategy.name}\" in the last {body.days} days — "
            "there is nothing to compare the backtest against. Let it trade for a while, widen the "
            "window, or import an export from another instance.",
        )

    # The replay is the ordinary backtest, unchanged and with the strategy's own
    # universe — anything else would be comparing against a different experiment.
    # Scanner strategies replay their real day-varying universe automatically.
    fee_pct = None  # let the backtest use the asset class's real rate
    spread_pct = 0.1
    scanner_replay = strategy.universe == "scanner"
    # The strategy's OWN universe, never the watchlist. run() falls back to the
    # watchlist when handed no symbols, which for a custom-universe strategy
    # would replay a different set of names than the one that produced these
    # trades — and then every mismatch would be an artefact of the substitution.
    symbols = [] if scanner_replay else _strategy_symbols(session, strategy)
    # The bar size the STRATEGY demands, not a hardcoded one. Asking for daily
    # bars on a strategy that needs intraday ones gets every entry rejected, and
    # the report would then read "the backtest missed all 20 trades" when the
    # truth is that it was handed the wrong resolution to make any.
    params = json.loads(strategy.params)
    entry = params.get("entry") or {}
    wants_intraday = bool(
        entry.get("require_above_vwap")
        or (entry.get("entry_window_start") and entry.get("entry_window_end"))
    )
    if _mixed_resolution(params) or (wants_intraday and not _uses_daily_only_signals(params)):
        timeframe = "15Min"
    else:
        timeframe = "1Day"
    result = await run(
        BacktestBody(
            strategy_id=strategy.id,
            symbols=symbols,
            days=body.days,
            scanner_replay=scanner_replay,
            timeframe=timeframe,
            starting_cash=max(strategy.sleeve_usd, 100),
            spread_pct=spread_pct,
            fee_pct=fee_pct,
        ),
        session=session,
        client=client,
    )

    report = fidelity.compare(
        live_rows,
        result,
        assumed_spread_pct=spread_pct,
        assumed_fee_pct=result.get("fee_pct_per_side") or 0.0,
        replayed_symbols=result.get("symbols") or [],
    )
    # When the replay traded NOTHING, the backtester already knows why — it
    # counts every rejection reason as it goes. Passing that through turns a
    # screen of "the backtest missed this" into the one sentence that explains
    # all of them at once.
    report["backtest_no_trade_reason"] = (result.get("diagnosis") or {}).get("summary")

    # CONFIG DRIFT. Every trade records the config version that produced it. The
    # replay uses the strategy as it stands TODAY, so if it was edited since —
    # a different universe, a bigger sleeve, more positions — the comparison is
    # answering "does today's strategy reproduce yesterday's trades", which is a
    # different and much less interesting question. It looks identical on screen,
    # which is why it has to be stated.
    version_ids = {r.get("config_version_id") for r in live_rows if r.get("config_version_id")}
    versions = (
        session.query(StrategyConfigVersion)
        .filter(StrategyConfigVersion.id.in_(version_ids))
        .order_by(StrategyConfigVersion.version_no)
        .all()
        if version_ids
        else []
    )
    produced_by = json.loads(versions[-1].snapshot) if versions else None
    # BASKET MEMBERSHIP. A strategy's config version records which basket it
    # points at, not who is in it — so a basket edit changes the universe while
    # leaving the strategy's own version identical. Without this the drift check
    # above would report "no change" and actively reassure you.
    basket_drift, basket_changes = _basket_drift(session, strategy, live_rows)
    report["config"] = {
        # More than one means the strategy was edited DURING the window, so no
        # single replay can be faithful to all of these trades — not even one
        # using an old config.
        "versions_used": len(versions),
        "produced_by_version": versions[-1].version_no if versions else None,
        "drift": fidelity.config_drift(produced_by, _serialize_current(strategy)) + basket_drift,
        # Basket edits made WHILE these trades were happening. Distinct from the
        # drift above: membership that changed and changed back looks identical
        # from any two points, so only a count can reveal it.
        "basket_changes_during_window": basket_changes,
    }
    report["strategy_name"] = strategy.name
    report["mode"] = body.mode
    report["days"] = body.days
    report["imported"] = body.imported_trades is not None
    report["timeframe"] = result.get("timeframe")
    # Gaps in the replay's data invalidate a mismatch before it means anything:
    # a "trade the backtest missed" on a day with no bars is a cache problem, not
    # a replay bug. Carried through so the UI can say which it is.
    report["bar_gaps"] = result.get("bar_gaps") or []
    # Paper fills are SIMULATED by the broker, so the execution half of this
    # report describes Alpaca's simulator rather than a real market. Decision
    # fidelity is unaffected — that is why paper is the sensible place to start.
    report["execution_is_measurable"] = body.mode == "live"
    return report


@router.get("/export")
def export(
    days: int = 90,
    mode: str = "live",
    session: Session = Depends(get_session),
) -> dict:
    """The journal for a window, as plain JSON, so another instance can diff
    against it. Trades only — no keys, no account numbers, no settings."""
    since = datetime.now(timezone.utc) - timedelta(days=days)
    rows = session.query(Trade).filter(Trade.mode == mode).all()
    strategies = {s.id: s for s in session.query(Strategy).all()}
    out = []
    for t in rows:
        stamp = _aware(t.entry_at) or _aware(t.created_at)
        if stamp is None or stamp < since:
            continue
        strategy = strategies.get(t.strategy_id)
        day_of = _day_of(strategy.asset_class if strategy else "stock")
        out.append(
            {
                "strategy_name": strategy.name if strategy else None,
                "symbol": t.symbol,
                "status": t.status,
                "entry_day": day_of(t.entry_at) if t.entry_at else None,
                "exit_day": day_of(t.exit_at) if t.exit_at else None,
                "entry_price": t.entry_price,
                "exit_price": t.exit_price,
                "pnl": t.pnl,
                "entry_reason": t.entry_reason,
                "exit_reason": t.exit_reason,
                "config_version_id": t.config_version_id,
            }
        )
    return {
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "mode": mode,
        "days": days,
        "trades": out,
    }
