"""Scheduled background jobs (besides the engine tick itself)."""

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import func

from qt.broker.factory import get_client
from qt.db import session_scope
from qt.models import Strategy, Trade
from qt.services import assets, notify, scoreboard
from qt.services.engine import get_mode

log = logging.getLogger("qt.jobs")


def _strategy_breakdown_lines(session, mode: str, since: datetime) -> str:
    """Per-strategy realized-P&L lines for a summary, newest strategies aside —
    ranked best first. Returns '' when there are no closed trades in the window
    (so the caller can append unconditionally)."""
    rows = (
        session.query(Trade.strategy_id, func.count(Trade.id), func.coalesce(func.sum(Trade.pnl), 0.0))
        .filter(Trade.mode == mode, Trade.status == "closed", Trade.exit_at >= since)
        .group_by(Trade.strategy_id)
        .all()
    )
    if not rows:
        return ""
    names = dict(session.query(Strategy.id, Strategy.name).all())
    rows = sorted(rows, key=lambda r: r[2], reverse=True)
    return "".join(
        f"\n• {names.get(sid, f'#{sid} (deleted)')}: ${pnl:,.2f} ({n} trade{'' if n == 1 else 's'})"
        for sid, n, pnl in rows
    )


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


async def backup_database() -> None:
    """Periodic snapshot of qt.db (config, keys, journal). Best-effort."""
    try:
        from qt.paths import data_dir
        from qt.services import backup

        backup.backup_db(data_dir())
    except Exception:
        log.exception("database backup failed")


async def reconcile_open_trades() -> None:
    """Reconcile the journal against Alpaca (startup + periodically). No-op when
    the engine is off or Alpaca isn't configured."""
    try:
        with session_scope() as session:
            mode = get_mode(session)
            if mode in ("off", "shadow"):
                return  # shadow places no real orders — nothing to reconcile
            client = get_client(session)
            if client is None:
                return
            from qt.services import reconcile

            await reconcile.apply_reconciliation(session, client, mode)
    except Exception:
        log.exception("reconciliation job failed")


async def daily_movers_sweep() -> None:
    """Keep the STOCK scanner-replay cache current: after the US close, pull the
    day's universe daily bars and re-rank the recent days' risers.

    Only MAINTAINS a cache the user already built — it never bootstraps one, so
    users who don't use scanner replay pay no daily Alpaca cost. Skips non-trading
    days (stocks don't trade weekends/holidays). Crypto is maintained separately
    by crypto_movers_sweep, which runs every calendar day. Best-effort; failures
    are logged, not fatal."""
    try:
        from qt.services import barcache, barsweep, calendar, scanner

        with session_scope() as session:
            client = get_client(session)
            if client is None:
                return
            try:
                if not await calendar.is_trading_today(client):
                    return  # holiday/weekend — no new stock bar to fetch
            except Exception:
                log.warning("daily movers sweep: calendar check failed, proceeding anyway")

            try:
                barcache.init_cache()
            except Exception:
                log.exception("daily movers sweep: could not open the bar cache")
                return
            sess = barcache.session()
            try:
                # STOCK cache — maintain only if it already exists.
                if sess.query(barcache.DailyBar).first() is not None:
                    f = scanner.STOCK_DEFAULTS
                    summary = await barsweep.daily_movers_update(
                        client, sess,
                        min_change_pct=f["min_change_pct"], min_price=f["min_price"],
                        max_price=f["max_price"], min_dollar_volume=f["min_dollar_volume"],
                    )
                    log.info("daily movers sweep done: %s", summary)
            finally:
                sess.close()
    except Exception:
        log.exception("daily movers sweep failed")


async def crypto_movers_sweep() -> None:
    """Keep the CRYPTO scanner-replay cache current — the 24/7 twin of
    daily_movers_sweep. Crypto never closes, so this runs EVERY calendar day
    (no US-trading-calendar gate), keeping weekend and holiday movers fresh
    instead of letting them lag until the next US trading day.

    Same 'maintain, never bootstrap' rule: a no-op unless the user has already
    built a crypto cache, so non-users pay nothing. The daily sweep skips the
    still-forming current UTC day and only caches completed days. Best-effort;
    failures are logged, not fatal."""
    try:
        from qt.services import barcache, barsweep, scanner

        with session_scope() as session:
            client = get_client(session)
            if client is None:
                return
            try:
                barcache.init_cache()
            except Exception:
                log.exception("crypto movers sweep: could not open the bar cache")
                return
            sess = barcache.session()
            try:
                if sess.query(barcache.CryptoDailyBar).first() is not None:
                    cf = scanner.CRYPTO_DEFAULTS
                    summary = await barsweep.crypto_daily_movers_update(
                        client, sess,
                        min_change_pct=cf["min_change_pct"], min_price=cf["min_price"],
                        max_price=cf["max_price"], min_dollar_volume=cf["min_dollar_volume"],
                    )
                    log.info("crypto movers sweep done: %s", summary)
            finally:
                sess.close()
    except Exception:
        log.exception("crypto movers sweep failed")


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
            if not notify.category_enabled(session, "daily_summary"):
                return  # user opted out of daily summaries
            client = get_client(session)
            # Skip the summary on days the market didn't trade (holidays). The
            # fixed 16:10 ET cron would otherwise post a meaningless "0 opened,
            # 0 closed" on every holiday. Half-days are fine: 16:10 is well past
            # the 13:00 close, so the data is complete. If we can't reach the
            # calendar, fall through and post anyway rather than go silent.
            if client is not None:
                from qt.services import calendar

                try:
                    if not await calendar.is_trading_today(client):
                        log.info("daily summary skipped: market closed today")
                        return
                except Exception:
                    log.warning("daily summary: calendar check failed, posting anyway")
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
            text = (
                f":newspaper: *QT daily summary* ({mode}): opened {opened}, closed {closed[0]} "
                f"for *${closed[1]:,.2f}* realized P&L; {open_now} position(s) still open."
            )
            if notify.category_enabled(session, "strategy_breakdown"):
                text += _strategy_breakdown_lines(session, mode, today)
            await notify.slack(session, text)
    except Exception:
        log.exception("daily summary failed")


async def weekly_summary() -> None:
    """A once-a-week recap of the last 7 days: trades closed, realized P&L, and
    win rate (plus a per-strategy breakdown if that's enabled). Opt-in — off by
    default. Not gated on the trading calendar: it's a rolling 7-day window and
    crypto trades every day, so it's meaningful whenever it's scheduled."""
    try:
        with session_scope() as session:
            mode = get_mode(session)
            if mode == "off":
                return
            if not notify.category_enabled(session, "weekly_summary"):
                return
            since = datetime.now(timezone.utc) - timedelta(days=7)
            n, pnl = (
                session.query(func.count(Trade.id), func.coalesce(func.sum(Trade.pnl), 0.0))
                .filter(Trade.mode == mode, Trade.status == "closed", Trade.exit_at >= since)
                .one()
            )
            wins = (
                session.query(func.count(Trade.id))
                .filter(Trade.mode == mode, Trade.status == "closed", Trade.exit_at >= since, Trade.pnl > 0)
                .scalar()
            )
            win_rate = f"{round(wins / n * 100)}%" if n else "—"
            text = (
                f":calendar: *QT weekly summary* ({mode}): {n} trade(s) closed in the last 7 days "
                f"for *${pnl:,.2f}* realized P&L · win rate {win_rate}."
            )
            if notify.category_enabled(session, "strategy_breakdown"):
                text += _strategy_breakdown_lines(session, mode, since)
            await notify.slack(session, text)
    except Exception:
        log.exception("weekly summary failed")
