import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import Depends, FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from qt import __version__
from qt.api import (
    assets as assets_api,
    auth,
    backtest,
    engine as engine_api,
    market,
    setup,
    status,
    strategies,
)
from qt.api.deps import leverage_unlockable, require_user
from qt.db import init_db

log = logging.getLogger("qt")


def _start_scheduler():
    from apscheduler.schedulers.asyncio import AsyncIOScheduler
    from apscheduler.triggers.cron import CronTrigger
    from apscheduler.triggers.interval import IntervalTrigger

    from qt.services import watchdog
    from qt.services.engine import tick
    from qt.services.jobs import (
        backup_database,
        daily_summary,
        reconcile_open_trades,
        snapshot_benchmarks,
        sync_assets,
    )

    scheduler = AsyncIOScheduler(timezone="UTC")

    async def engine_tick():
        try:
            await tick(leverage_unlocked=leverage_unlockable())
        except Exception:
            log.exception("engine tick failed")

    scheduler.add_job(engine_tick, IntervalTrigger(seconds=60), max_instances=1, coalesce=True)
    scheduler.add_job(snapshot_benchmarks, IntervalTrigger(minutes=60), max_instances=1)
    scheduler.add_job(
        daily_summary,
        CronTrigger(day_of_week="mon-fri", hour=16, minute=10, timezone="America/New_York"),
    )
    # Symbol directory: shortly after boot, then hourly. Each run is a no-op
    # unless the directory is empty or >24h old, so this is really "refresh
    # daily, but retry within the hour if a sync fails" rather than waiting a
    # full day after one bad download.
    scheduler.add_job(sync_assets, "date", run_date=datetime.now(timezone.utc) + timedelta(seconds=20))
    scheduler.add_job(sync_assets, IntervalTrigger(hours=1), max_instances=1)
    # Crash-recovery: reconcile the journal against Alpaca shortly after boot,
    # then every 15 minutes as a backstop for missed exits / orphaned orders.
    scheduler.add_job(
        reconcile_open_trades, "date", run_date=datetime.now(timezone.utc) + timedelta(seconds=10)
    )
    scheduler.add_job(reconcile_open_trades, IntervalTrigger(minutes=15), max_instances=1, coalesce=True)
    # Watchdog: alert once if the engine heartbeat goes stale while the market
    # is open. Runs every 5 minutes.
    scheduler.add_job(watchdog.check, IntervalTrigger(minutes=5), max_instances=1, coalesce=True)
    # Nightly DB backup (03:15 ET, a quiet hour) + one shortly after boot so a
    # fresh container has a restore point before the first trading day.
    scheduler.add_job(
        backup_database,
        CronTrigger(hour=3, minute=15, timezone="America/New_York"),
        max_instances=1,
    )
    scheduler.add_job(
        backup_database, "date", run_date=datetime.now(timezone.utc) + timedelta(seconds=30)
    )
    scheduler.start()
    return scheduler


async def _startup_checks() -> None:
    """Boot-time data-integrity checks: is /data persistent, and are the
    encrypted secrets still decryptable? Log loudly and Slack-alert on any
    problem — these are the failures that silently destroy Werner's data."""
    from qt.db import session_scope
    from qt.models import Secret
    from qt.paths import data_dir
    from qt.services import notify, persistence

    with session_scope() as session:
        secrets_count = session.query(Secret).count()
        state = persistence.capture_boot_state(data_dir(), secrets_count)

        if state["data_persistent"] is False:
            log.error(
                "DATA PERSISTENCE WARNING: %s. Configuration, keys and trade "
                "history will be LOST when this container updates. Fix the "
                "volume mapping (host path -> /data). See docs/data-persistence.md.",
                state["data_persistent_reason"],
            )
            await notify.slack(
                session,
                ":rotating_light: *QT data directory is NOT persistent* — "
                f"{state['data_persistent_reason']}. Configuration, API keys and "
                "trade history will be lost the next time the container updates. "
                "Fix the `/data` volume mapping now.",
            )
        elif state["data_persistent"] is True:
            log.info("Data persistence OK: %s", state["data_persistent_reason"])

        if state["secrets_without_key"]:
            log.error(
                "SECRETS/KEY MISMATCH: the database holds encrypted secrets but "
                "%s is missing. The secrets cannot be decrypted. Restore the "
                "original instance.key, or clear secrets and re-enter your "
                "Alpaca keys.",
                data_dir() / "instance.key",
            )
            await notify.slack(
                session,
                ":rotating_light: *QT cannot decrypt its secrets* — the database "
                "has encrypted API keys but `instance.key` is missing. Restore "
                "the key file or re-run setup.",
            )


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    try:
        await _startup_checks()
    except Exception:
        log.exception("startup checks failed (non-fatal)")
    scheduler = None
    if os.environ.get("QT_NO_SCHEDULER", "").lower() != "true":
        scheduler = _start_scheduler()
    yield
    if scheduler:
        await _graceful_shutdown(scheduler)


SHUTDOWN_GRACE_SECONDS = 20


async def _graceful_shutdown(scheduler) -> None:
    """Let an in-flight engine tick (esp. an order submit->confirm) finish
    before we exit. Set the shutdown flag first so no NEW entries begin, then
    wait for running jobs — bounded, so a wedged job can't hang the container."""
    import asyncio

    from qt.services import lifecycle

    lifecycle.request_shutdown()
    loop = asyncio.get_running_loop()
    try:
        await asyncio.wait_for(
            loop.run_in_executor(None, lambda: scheduler.shutdown(wait=True)),
            timeout=SHUTDOWN_GRACE_SECONDS,
        )
    except asyncio.TimeoutError:
        log.warning(
            "graceful shutdown timed out after %ss — forcing scheduler stop",
            SHUTDOWN_GRACE_SECONDS,
        )
        try:
            scheduler.shutdown(wait=False)
        except Exception:
            pass


app = FastAPI(title="QT Auto-Trader", version=__version__, lifespan=lifespan)
app.include_router(auth.router)  # login/bootstrap must work pre-auth
app.include_router(setup.router, dependencies=[Depends(require_user)])
app.include_router(status.router)  # /api/health public; /api/status gated in-module
app.include_router(market.router, dependencies=[Depends(require_user)])
app.include_router(strategies.router, dependencies=[Depends(require_user)])
app.include_router(engine_api.router, dependencies=[Depends(require_user)])
app.include_router(backtest.router, dependencies=[Depends(require_user)])
app.include_router(assets_api.router, dependencies=[Depends(require_user)])


def _static_dir() -> Path | None:
    raw = os.environ.get("QT_STATIC_DIR")
    candidates = [Path(raw)] if raw else []
    candidates.append(Path(__file__).resolve().parent.parent.parent / "frontend" / "dist")
    for candidate in candidates:
        if candidate.is_dir():
            return candidate
    return None


static = _static_dir()
if static:
    app.mount("/assets", StaticFiles(directory=static / "assets"), name="assets")

    @app.get("/{full_path:path}", include_in_schema=False)
    def spa(full_path: str) -> FileResponse:
        candidate = static / full_path
        if full_path and candidate.is_file():
            return FileResponse(candidate)
        return FileResponse(static / "index.html")
