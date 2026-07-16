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

    from qt.services.engine import tick
    from qt.services.jobs import daily_summary, snapshot_benchmarks, sync_assets

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
    # Symbol directory: shortly after boot (no-op unless empty/stale), then daily.
    scheduler.add_job(sync_assets, "date", run_date=datetime.now(timezone.utc) + timedelta(seconds=20))
    scheduler.add_job(sync_assets, CronTrigger(hour=8, minute=0, timezone="America/New_York"))
    scheduler.start()
    return scheduler


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    scheduler = None
    if os.environ.get("QT_NO_SCHEDULER", "").lower() != "true":
        scheduler = _start_scheduler()
    yield
    if scheduler:
        scheduler.shutdown(wait=False)


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
