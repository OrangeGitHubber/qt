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
    kind: str = "daily"  # daily | reconstruct | intraday — what the current/last run was
    # True when the nightly upkeep started this rather than the user. Only ever
    # set on a run, so the Settings view can label the one it is watching.
    automatic: bool = False
    market: str = "stock"  # stock | crypto — which cache the current/last run touched
    started_at: str | None = None
    last_run_at: str | None = None
    phase: str = ""  # reconstruct sub-phase: "loading bars" | "ranking days"
    batches_total: int = 0
    batches_done: int = 0
    symbols_total: int = 0
    symbols_saved: int = 0
    days_reconstructed: int = 0
    intraday_bars: int = 0
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


def _reconstruct_progress(stage: str, done: int, total: int) -> None:
    """Mirror reconstruct's two phases into the shared status (safe to write from
    the worker thread — simple attribute assignments the status reader picks up)."""
    _progress.phase = "loading bars" if stage == "load" else "ranking days"
    _progress.batches_done = done
    _progress.batches_total = total


def _reconstruct(sess) -> int:
    """Re-rank every cached day's movers with the live stock scanner's filters,
    storing a generous set (SWEEP_STORE_TOP_N) so the backtest can pick its own
    riser count at read time. Shared by the full sweep and the reconstruct-only
    endpoint."""
    f = scanner.STOCK_DEFAULTS
    return barsweep.reconstruct_movers(
        sess,
        top_n=barsweep.SWEEP_STORE_TOP_N,
        min_change_pct=f["min_change_pct"],
        min_price=f["min_price"],
        max_price=f["max_price"],
        min_dollar_volume=f["min_dollar_volume"],
        progress=_reconstruct_progress,
    )


def _reconstruct_blocking() -> int:
    """Reconstruct in a WORKER THREAD with its own session. reconstruct_movers is
    CPU-and-DB heavy (it loads every cached daily bar and ranks each day in pure
    Python); running it inline in the async task would block the event loop and
    freeze the whole app — no status updates, no other endpoints — until it
    finished. Offloading keeps the server responsive."""
    sess = barcache.session()
    try:
        return _reconstruct(sess)
    finally:
        sess.close()


def _reconstruct_crypto(sess) -> int:
    """Re-rank every cached crypto day's movers with the live crypto scanner's
    filters (CRYPTO_DEFAULTS), storing a generous set so the backtest picks its
    own riser count at read time."""
    f = scanner.CRYPTO_DEFAULTS
    return barsweep.reconstruct_crypto_movers(
        sess,
        top_n=barsweep.SWEEP_STORE_TOP_N,
        min_change_pct=f["min_change_pct"],
        min_price=f["min_price"],
        max_price=f["max_price"],
        min_dollar_volume=f["min_dollar_volume"],
        progress=_reconstruct_progress,
    )


def _reconstruct_crypto_blocking() -> int:
    """reconstruct crypto movers in a worker thread (see _reconstruct_blocking)."""
    sess = barcache.session()
    try:
        return _reconstruct_crypto(sess)
    finally:
        sess.close()


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

        _progress.days_reconstructed = await asyncio.to_thread(_reconstruct_blocking)
    except Exception as exc:  # noqa: BLE001 — record any failure for the status view
        log.exception("bar sweep failed")
        _progress.last_error = str(exc)
    finally:
        sess.close()
        _progress.running = False
        _progress.last_run_at = datetime.now(timezone.utc).isoformat()


async def _run_reconstruct() -> None:
    """Background worker: re-rank movers from the bars ALREADY cached — no
    download. Cheap way to apply new scanner filters, or to widen the stored
    riser set, across the whole history."""
    try:
        _progress.days_reconstructed = await asyncio.to_thread(_reconstruct_blocking)
    except Exception as exc:  # noqa: BLE001
        log.exception("movers reconstruct failed")
        _progress.last_error = str(exc)
    finally:
        _progress.running = False
        _progress.last_run_at = datetime.now(timezone.utc).isoformat()


async def _run_intraday_sweep(client: AlpacaClient) -> None:
    """Background worker: pull intraday (15-min) bars for the reconstructed
    movers so intraday strategies can be replayed on how each day unfolded."""
    sess = barcache.session()
    try:
        def on_progress(done: int, total: int, saved: int, bars: int) -> None:
            _progress.batches_done = done
            _progress.batches_total = total
            _progress.symbols_saved = saved
            _progress.intraday_bars = bars  # live count, not just the final total

        summary = await barsweep.sweep_intraday_movers(client, sess, progress=on_progress)
        _progress.batches_total = summary["days"]
        _progress.batches_done = summary["days"]
        _progress.symbols_saved = summary["symbols_saved"]
        _progress.intraday_bars = summary["bars_saved"]
        _progress.errors = summary["errors"]
    except Exception as exc:  # noqa: BLE001
        log.exception("intraday sweep failed")
        _progress.last_error = str(exc)
    finally:
        sess.close()
        _progress.running = False
        _progress.last_run_at = datetime.now(timezone.utc).isoformat()


async def _run_crypto_sweep(client: AlpacaClient, days: int) -> None:
    """Background worker: sweep the whole crypto USD-pair universe (daily), then
    reconstruct crypto movers using the live crypto scanner's filters."""
    sess = barcache.session()
    try:
        def on_progress(done: int, total: int, saved: int) -> None:
            _progress.batches_done = done
            _progress.batches_total = total
            _progress.symbols_saved = saved

        summary = await barsweep.sweep_crypto_daily_bars(client, sess, days=days, progress=on_progress)
        _progress.symbols_total = summary["symbols_total"]
        _progress.symbols_saved = summary["symbols_saved"]
        _progress.batches_total = summary["batches"]
        _progress.batches_done = summary["batches"]
        _progress.errors = summary["errors"]

        _progress.days_reconstructed = await asyncio.to_thread(_reconstruct_crypto_blocking)
    except Exception as exc:  # noqa: BLE001
        log.exception("crypto bar sweep failed")
        _progress.last_error = str(exc)
    finally:
        sess.close()
        _progress.running = False
        _progress.last_run_at = datetime.now(timezone.utc).isoformat()


async def _run_crypto_intraday_sweep(client: AlpacaClient) -> None:
    """Background worker: pull 15-min bars for every crypto mover-day so crypto
    scanner replay can run on real intraday data."""
    sess = barcache.session()
    try:
        def on_progress(done: int, total: int, saved: int, bars: int) -> None:
            _progress.batches_done = done
            _progress.batches_total = total
            _progress.symbols_saved = saved
            _progress.intraday_bars = bars

        summary = await barsweep.sweep_crypto_intraday(client, sess, progress=on_progress)
        _progress.batches_total = summary["days"]
        _progress.batches_done = summary["days"]
        _progress.symbols_saved = summary["symbols_saved"]
        _progress.intraday_bars = summary["bars_saved"]
        _progress.errors = summary["errors"]
    except Exception as exc:  # noqa: BLE001
        log.exception("crypto intraday sweep failed")
        _progress.last_error = str(exc)
    finally:
        sess.close()
        _progress.running = False
        _progress.last_run_at = datetime.now(timezone.utc).isoformat()


async def bootstrap(client: AlpacaClient, market: str, days: int = 365) -> bool:
    """Build a scanner-replay cache from nothing, unattended.

    This is the Settings button's whole job, minus the button. It is called by
    the nightly upkeep when a strategy needs a cache that does not exist yet, so
    "run the sweep once before you can backtest a scanner strategy" stopped being
    something the user has to know. It shares the same progress record the manual
    trigger writes, so the Settings view narrates an automatic build exactly as it
    narrates a requested one.

    Returns False when a sweep is already running — the caller must not queue a
    second one, and the next night's run will find the cache either built or
    still empty and decide again. Intraday bars are deliberately NOT swept here:
    a backtest fetches the 15-minute bars it needs for itself, so pre-fetching
    them would spend the API budget on days no strategy ever asks about."""
    global _task
    if _progress.running:
        return False
    try:
        barcache.init_cache()
    except Exception:
        log.exception("automatic %s sweep: could not open the bar cache", market)
        return False

    _progress.running = True
    _progress.kind = "daily"
    _progress.market = market
    _progress.automatic = True
    _progress.started_at = datetime.now(timezone.utc).isoformat()
    _progress.batches_done = 0
    _progress.batches_total = 0
    _progress.days_reconstructed = 0
    _progress.last_error = None
    log.info("no %s scanner-replay cache and a strategy needs one — building it (%sd)", market, days)
    # Awaited, not fired off as a task: the caller is a scheduled job with
    # max_instances=1, which is the lock that stops two builds overlapping.
    if market == "crypto":
        await _run_crypto_sweep(client, days)
    else:
        await _run_sweep(client, days)
    return _progress.last_error is None


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
    _progress.kind = "daily"
    _progress.market = "stock"
    _progress.automatic = False
    _progress.started_at = datetime.now(timezone.utc).isoformat()
    _progress.batches_done = 0
    _progress.batches_total = 0
    _progress.days_reconstructed = 0
    _progress.last_error = None
    _task = asyncio.create_task(_run_sweep(client, days))
    return {"ok": True, "started": True, "days": days, "backend": _backend_info()}


@router.post("/reconstruct")
async def trigger_reconstruct() -> dict:
    """Re-rank the movers from bars ALREADY in the cache — no re-download.

    Use after changing the scanner's filters, or to (re)build the generous
    stored riser set on a cache swept before that was the default. Seconds, not
    the full sweep. Returns immediately; poll /status."""
    global _task
    if _progress.running:
        raise HTTPException(status_code=409, detail="A sweep or reconstruct is already running.")
    try:
        barcache.init_cache()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"Could not open the bar cache DB: {exc}")

    _progress.running = True
    _progress.kind = "reconstruct"
    _progress.market = "stock"
    _progress.phase = "loading bars"
    _progress.started_at = datetime.now(timezone.utc).isoformat()
    _progress.batches_done = 0
    _progress.batches_total = 0
    _progress.days_reconstructed = 0
    _progress.last_error = None
    _task = asyncio.create_task(_run_reconstruct())
    return {"ok": True, "started": True, "reconstruct_only": True, "backend": _backend_info()}


@router.post("/sweep-intraday")
async def trigger_intraday_sweep(
    client: AlpacaClient = Depends(require_client),
) -> dict:
    """Pull intraday (15-min) bars for the reconstructed movers, so scanner
    replay can judge an intraday strategy on how each day actually traded
    (flatten-before-close, VWAP, the entry window). Run a daily sweep first so
    there are movers to fetch. Returns immediately; poll /status."""
    global _task
    if _progress.running:
        raise HTTPException(status_code=409, detail="A sweep or reconstruct is already running.")
    try:
        barcache.init_cache()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"Could not open the bar cache DB: {exc}")

    _progress.running = True
    _progress.kind = "intraday"
    _progress.market = "stock"
    _progress.started_at = datetime.now(timezone.utc).isoformat()
    _progress.batches_done = 0
    _progress.batches_total = 0
    _progress.symbols_saved = 0
    _progress.symbols_total = 0  # not a symbol-total concept for intraday (batched by day)
    _progress.intraday_bars = 0
    _progress.last_error = None
    _task = asyncio.create_task(_run_intraday_sweep(client))
    return {"ok": True, "started": True, "intraday": True, "backend": _backend_info()}


@router.post("/sweep-crypto")
async def trigger_crypto_sweep(
    days: int = Query(default=365, ge=7, le=1825),
    client: AlpacaClient = Depends(require_client),
) -> dict:
    """Kick off the crypto USD-pair sweep + crypto movers reconstruction as a
    background task. The crypto universe is tiny, so this pulls EVERY pair's
    daily bars (not just movers) and ranks each UTC day's risers. Returns
    immediately; poll /status."""
    global _task
    if _progress.running:
        raise HTTPException(status_code=409, detail="A sweep is already running.")
    try:
        barcache.init_cache()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"Could not open the bar cache DB: {exc}")

    _progress.running = True
    _progress.kind = "daily"
    _progress.market = "crypto"
    _progress.started_at = datetime.now(timezone.utc).isoformat()
    _progress.batches_done = 0
    _progress.batches_total = 0
    _progress.days_reconstructed = 0
    _progress.last_error = None
    _task = asyncio.create_task(_run_crypto_sweep(client, days))
    return {"ok": True, "started": True, "crypto": True, "days": days, "backend": _backend_info()}


@router.post("/sweep-crypto-intraday")
async def trigger_crypto_intraday_sweep(
    client: AlpacaClient = Depends(require_client),
) -> dict:
    """Pull 15-min bars for the reconstructed crypto movers so crypto scanner
    replay can judge an intraday strategy on how each day actually traded. Run a
    crypto sweep first so there are movers to fetch. Returns immediately; poll
    /status."""
    global _task
    if _progress.running:
        raise HTTPException(status_code=409, detail="A sweep or reconstruct is already running.")
    try:
        barcache.init_cache()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"Could not open the bar cache DB: {exc}")

    _progress.running = True
    _progress.kind = "intraday"
    _progress.market = "crypto"
    _progress.started_at = datetime.now(timezone.utc).isoformat()
    _progress.batches_done = 0
    _progress.batches_total = 0
    _progress.symbols_saved = 0
    _progress.symbols_total = 0
    _progress.intraday_bars = 0
    _progress.last_error = None
    _task = asyncio.create_task(_run_crypto_intraday_sweep(client))
    return {"ok": True, "started": True, "crypto": True, "intraday": True, "backend": _backend_info()}


@router.get("/status")
def sweep_status() -> dict:
    has_intraday = False
    crypto_has_intraday = False
    cache = None
    crypto_cache = None
    try:
        sess = barcache.session()
        try:
            has_intraday = barcache.has_intraday(sess)
            crypto_has_intraday = barcache.has_intraday(sess, model=barcache.CryptoIntradayBar)
            # Persisted totals — the real state of the (durable) cache, so a
            # redeploy shows what's there, not zeros. Skipped mid-sweep: the live
            # in-memory counters cover that, and count(*) would hammer the DB
            # every poll.
            if not _progress.running:
                cache = barcache.cache_stats(sess)
                crypto_cache = barcache.crypto_cache_stats(sess)
        finally:
            sess.close()
    except Exception:  # noqa: BLE001 — a bad/unbuilt cache just means "nothing cached yet"
        has_intraday = False
        crypto_has_intraday = False
    return {
        **asdict(_progress),
        "has_intraday": has_intraday,
        "crypto_has_intraday": crypto_has_intraday,
        "cache": cache,
        "crypto_cache": crypto_cache,
        "backend": _backend_info(),
    }
