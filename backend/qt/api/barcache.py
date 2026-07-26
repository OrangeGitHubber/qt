"""Trigger + status for the historical bar sweep / movers reconstruction.

POST /api/barcache/sweep kicks the (heavy) universe sweep off as a background
task and returns immediately; GET /api/barcache/status reports progress. Only
one sweep runs at a time. Progress is tracked in a simple in-process dataclass
— it resets on restart, which is fine for v1 (the sweep is idempotent, so a
re-run after a restart just refills the cache).
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, Query

from qt.api.market import require_client
from qt.broker.alpaca import AlpacaClient
from qt.paths import bar_cache_url
from qt.services import barcache, barsweep, scanner

log = logging.getLogger("qt.api.barcache")

router = APIRouter(prefix="/api/barcache", tags=["barcache"])


@dataclass
class SweepProgress:
    running: bool = False
    started_at: str | None = None
    last_run_at: str | None = None
    batches_total: int = 0
    batches_done: int = 0
    symbols_total: int = 0
    symbols_saved: int = 0
    days_reconstructed: int = 0
    errors: int = 0
    last_error: str | None = None


_progress = SweepProgress()
_task: asyncio.Task | None = None  # keep a ref so the task isn't GC'd mid-run


def _backend_info() -> dict:
    """Cache-DB identity for the UI — scheme/host only, NEVER the password."""
    parts = urlsplit(bar_cache_url())
    scheme = parts.scheme or "sqlite"
    return {
        "kind": "postgres" if scheme.startswith("postgres") else "sqlite",
        "scheme": scheme,
        "host": parts.hostname,
    }


async def _run_sweep(client: AlpacaClient, days: int) -> None:
    """Background worker: sweep the universe, then reconstruct movers using the
    same defaults the live stock scanner applies."""
    sess = barcache.session()
    try:
        def on_progress(done: int, total: int, saved: int) -> None:
            _progress.batches_done = done
            _progress.batches_total = total
            _progress.symbols_saved = saved

        summary = await barsweep.sweep_daily_bars(client, sess, days=days, progress=on_progress)
        _progress.symbols_total = summary["symbols_total"]
        _progress.symbols_saved = summary["symbols_saved"]
        _progress.batches_total = summary["batches"]
        _progress.batches_done = summary["batches"]
        _progress.errors = summary["errors"]

        f = scanner.STOCK_DEFAULTS
        _progress.days_reconstructed = barsweep.reconstruct_movers(
            sess,
            top_n=scanner.DEFAULT_CONFIG["top_n"],
            min_change_pct=f["min_change_pct"],
            min_price=f["min_price"],
            max_price=f["max_price"],
            min_dollar_volume=f["min_dollar_volume"],
        )
    except Exception as exc:  # noqa: BLE001 — record any failure for the status view
        log.exception("bar sweep failed")
        _progress.last_error = str(exc)
    finally:
        sess.close()
        _progress.running = False
        _progress.last_run_at = datetime.now(timezone.utc).isoformat()


@router.post("/sweep")
async def trigger_sweep(
    days: int = Query(default=365, ge=7, le=1825),
    client: AlpacaClient = Depends(require_client),
) -> dict:
    """Kick off the universe sweep + movers reconstruction as a background task.

    Creates the cache tables first (in the configured SQLite/Postgres DB), which
    also confirms the connection works. Returns immediately; poll /status."""
    global _task
    if _progress.running:
        raise HTTPException(status_code=409, detail="A sweep is already running.")

    try:
        barcache.init_cache()
    except Exception as exc:  # noqa: BLE001 — surface a bad cache DSN clearly
        raise HTTPException(status_code=502, detail=f"Could not open the bar cache DB: {exc}")

    _progress.running = True
    _progress.started_at = datetime.now(timezone.utc).isoformat()
    _progress.batches_done = 0
    _progress.batches_total = 0
    _progress.days_reconstructed = 0
    _progress.last_error = None
    _task = asyncio.create_task(_run_sweep(client, days))
    return {"ok": True, "started": True, "days": days, "backend": _backend_info()}


@router.get("/status")
def sweep_status() -> dict:
    return {**asdict(_progress), "backend": _backend_info()}
