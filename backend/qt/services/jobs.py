"""Scheduled background jobs (besides the engine tick itself)."""

import logging
from datetime import datetime, timezone

from sqlalchemy import func

from qt.broker.factory import get_client
from qt.db import session_scope
from qt.models import Trade
from qt.services import assets, notify, scoreboard
from qt.services.engine import get_mode

log = logging.getLogger("qt.jobs")


async def sync_assets(force: bool = False) -> None:
    """Refresh the symbol directory when empty or stale (>24h)."""
    try:
        with session_scope() as session:
            if not force and not assets.status(session)["stale"]:
                return
            client = get_client(session)
            if client is None:
                log.info("asset sync skipped: Alpaca not configured yet")
                return
            log.info("asset directory sync starting…")
            await assets.sync(session, client)
    except Exception:
        # Left stale on purpose: the hourly job retries, and the UI shows a
        # "needs sync" badge with a manual button rather than failing silently.
        log.exception("asset directory sync failed — will retry within the hour")


async def snapshot_benchmarks() -> None:
    try:
        with session_scope() as session:
            client = get_client(session)
            if client:
                await scoreboard.record_snapshot(session, client)
    except Exception:
        log.exception("benchmark snapshot failed")


async def daily_summary() -> None:
    try:
        with session_scope() as session:
            mode = get_mode(session)
            if mode == "off":
                return
            today = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
            closed = (
                session.query(func.count(Trade.id), func.coalesce(func.sum(Trade.pnl), 0.0))
                .filter(Trade.mode == mode, Trade.status == "closed", Trade.exit_at >= today)
                .one()
            )
            opened = (
                session.query(func.count(Trade.id))
                .filter(Trade.mode == mode, Trade.entry_at >= today, Trade.status != "rejected")
                .scalar()
            )
            open_now = (
                session.query(func.count(Trade.id))
                .filter(Trade.mode == mode, Trade.status == "open")
                .scalar()
            )
            await notify.slack(
                session,
                f":newspaper: *QT daily summary* ({mode}): opened {opened}, closed {closed[0]} "
                f"for *${closed[1]:,.2f}* realized P&L; {open_now} position(s) still open.",
            )
    except Exception:
        log.exception("daily summary failed")
