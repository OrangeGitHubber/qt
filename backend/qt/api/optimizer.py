"""Trigger + status for the strategy parameter search (Phase 4).

POST /api/optimizer kicks the (heavy) search off as a background task and
returns immediately; GET /api/optimizer/status reports progress and, when
finished, the full result. Only one search runs at a time. Progress lives in a
simple in-process dataclass — it resets on restart, which is fine: the search
is a read-only experiment, so a re-run after a restart just re-computes.

Mirrors the background-task + in-process progress + POST-to-start +
GET-/status shape of qt.api.barcache. The search itself is in
qt.services.optimizer and reuses run_backtest unchanged.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from qt.api.market import require_client
from qt.broker.alpaca import AlpacaClient, AlpacaError
from qt.db import get_session
from qt.models import BasketItem, Strategy, WatchlistItem
from qt.services import optimizer
from qt.services.engine import get_risk

log = logging.getLogger("qt.api.optimizer")

router = APIRouter(prefix="/api/optimizer", tags=["optimizer"])


@dataclass
class OptimizeProgress:
    running: bool = False
    phase: str = ""  # "downloading bars" | "searching" | "validating" | "done"
    strategy_name: str = ""
    started_at: str | None = None
    finished_at: str | None = None
    combos_total: int = 0
    combos_done: int = 0
    error: str | None = None
    result: dict | None = field(default=None)


_progress = OptimizeProgress()
_task: asyncio.Task | None = None  # keep a ref so the task isn't GC'd mid-run


class OptimizeBody(BaseModel):
    strategy_id: int
    symbols: list[str] = []  # empty = the strategy's own universe / its asset-class watchlist
    scanner_replay: bool = False  # search against the cached historical daily top-N risers
    replay_top_n: int = Field(default=10, ge=1, le=100)  # how many of each day's risers are eligible
    days: int = Field(default=180, ge=30, le=730)
    timeframe: str = Field(default="1Day", pattern="^(15Min|1Hour|1Day)$")
    iterations: int = Field(default=40, ge=5, le=200)
    starting_cash: float = Field(default=5000, ge=100, le=10_000_000)
    spread_pct: float = Field(default=0.1, ge=0, le=2)


def _resolve_symbols(session: Session, strategy: Strategy, requested: list[str]) -> list[str]:
    """Symbols to validate the search across. Explicit picks win; otherwise fall
    back to the strategy's own universe — a custom list as-is, a basket's members,
    or the asset-class watchlist. A merged historical timeline can't reconstruct
    the scanner's daily picks, so a scanner strategy validates on its watchlist
    (the same honest limitation the portfolio backtest carries)."""
    picked = [s.strip().upper() for s in requested if s.strip()]
    if picked:
        return sorted(set(picked))
    if strategy.universe == "custom":
        return sorted({s.strip().upper() for s in (json.loads(strategy.symbols) if strategy.symbols else []) if s.strip()})
    if strategy.universe == "basket" and strategy.basket_id is not None:
        return sorted(
            {
                i.symbol
                for i in session.query(BasketItem).filter(
                    BasketItem.basket_id == strategy.basket_id,
                    BasketItem.asset_class == strategy.asset_class,
                )
            }
        )
    return sorted(
        {
            i.symbol
            for i in session.query(WatchlistItem).filter(
                WatchlistItem.asset_class == strategy.asset_class
            )
        }
    )


async def _run_search(
    client: AlpacaClient,
    strategy_dict: dict,
    risk: dict,
    symbols: list[str],
    asset_class: str,
    timeframe: str,
    days: int,
    iterations: int,
    starting_cash: float,
    spread_pct: float,
    prebuilt_bars: dict | None = None,
    eligible_by_day: dict | None = None,
    replay_extra: dict | None = None,
) -> None:
    """Background worker: get the bars once, then run the search in a worker
    thread (it is CPU-heavy — dozens of full backtests — so it must not block the
    event loop) with a progress callback the status endpoint reads.

    Fixed-universe mode downloads the bars from Alpaca. Scanner-replay mode
    passes `prebuilt_bars` (read offline from the cache) + `eligible_by_day` (each
    day's top-N risers) so every backtest can only ENTER a symbol on the days it
    actually rose — the search then optimizes the strategy against its real
    universe, not a stand-in watchlist."""
    try:
        if prebuilt_bars is not None:
            bars = prebuilt_bars
        else:
            _progress.phase = "downloading bars"
            start = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
            bars = await client.historical_bars(symbols, asset_class, timeframe, start)
            if not any(bars.get(s) for s in symbols):
                raise ValueError("No historical bars for those symbols/timeframe.")

        _progress.phase = "searching"

        def on_progress(done: int, total: int) -> None:
            _progress.combos_done = done
            _progress.combos_total = total

        market = "crypto" if asset_class == "crypto" else "stock"
        result = await asyncio.to_thread(
            optimizer.optimize,
            strategy_dict,
            bars,
            risk,
            iterations=iterations,
            starting_cash=starting_cash,
            spread_pct=spread_pct,
            market=market,
            eligible_by_day=eligible_by_day,
            progress=on_progress,
        )
        result["strategy_name"] = _progress.strategy_name
        result["timeframe"] = timeframe
        result["days"] = days
        if replay_extra:
            result.update(replay_extra)
        _progress.result = result
        _progress.combos_done = result["tested_combinations"]
        _progress.combos_total = result["tested_combinations"]
        _progress.phase = "done"
    except AlpacaError as exc:
        log.exception("optimizer bar download failed")
        _progress.error = f"Bar download failed ({exc.status_code}): {exc}"
    except Exception as exc:  # noqa: BLE001 — record any failure for the status view
        log.exception("optimizer search failed")
        _progress.error = str(exc)
    finally:
        _progress.running = False
        _progress.finished_at = datetime.now(timezone.utc).isoformat()


@router.post("")
async def start_optimize(
    body: OptimizeBody,
    session: Session = Depends(get_session),
    client: AlpacaClient = Depends(require_client),
) -> dict:
    """Kick off the parameter search as a background task. Returns immediately;
    poll /status for progress and the final result. Only one search at a time."""
    global _task
    if _progress.running:
        raise HTTPException(status_code=409, detail="A parameter search is already running.")

    strategy = session.get(Strategy, body.strategy_id)
    if not strategy:
        raise HTTPException(status_code=404, detail="Strategy not found.")

    # Scanner-replay mode reads its universe (each past day's top-N risers) +
    # their bars OFFLINE from the bar cache; the fixed-universe mode resolves a
    # symbol list and downloads bars from Alpaca in the background task.
    prebuilt_bars: dict | None = None
    eligible_by_day: dict | None = None
    replay_extra: dict | None = None
    timeframe = body.timeframe
    if body.scanner_replay:
        from qt.api.backtest import load_scanner_replay_dataset

        ds = load_scanner_replay_dataset(strategy.asset_class, body.days, body.replay_top_n)
        prebuilt_bars = ds.bars
        eligible_by_day = ds.eligible_by_day
        timeframe = ds.timeframe  # 15Min if intraday cached, else 1Day — from the cache
        symbols = ds.union  # the deduped set of names that made a top-N list (offline: no 25 cap)
        replay_extra = {
            "scanner_replay": True,
            "replay_intraday": ds.used_intraday,
            "replay_top_n": body.replay_top_n,
            "universe_size": len(ds.union),
            "days_replayed": ds.days_replayed,
        }
    else:
        symbols = _resolve_symbols(session, strategy, body.symbols)
        if not symbols:
            raise HTTPException(
                status_code=422,
                detail="No symbols to search over — pass some, or add symbols to the watchlist for this asset class.",
            )
        # The search downloads the bars ONCE (batched) then reuses them across every
        # iteration, so the symbol count only affects that one download — 50 covers a
        # full sector basket with headroom without straining the rate limit.
        if len(symbols) > 50:
            raise HTTPException(status_code=422, detail="Max 50 symbols per search (rate limits).")
        # VWAP is an intraday measure — on 1Day bars the "price above VWAP" entry
        # rule can't be evaluated meaningfully, so every entry is rejected and the
        # whole search comes back with 0 trades (looks like the strategy is broken
        # when it's really the timeframe). Fail fast with the same guidance the
        # backtest gives, rather than burn a run on a guaranteed-empty result.
        if body.timeframe == "1Day" and json.loads(strategy.params).get("entry", {}).get("require_above_vwap"):
            raise HTTPException(
                status_code=422,
                detail="This strategy needs price above VWAP, which requires intraday bars — "
                "pick 1Hour or 15Min for the search, or turn the VWAP rule off.",
            )

    # Read everything the (session-less) background task needs NOW, while the
    # request's DB session is open — pass plain dicts/lists into the task.
    strategy_dict = {
        "asset_class": strategy.asset_class,
        "swing_mode": strategy.swing_mode,
        "sizing_usd": strategy.sizing_usd,
        "sleeve_usd": strategy.sleeve_usd,
        "max_positions": strategy.max_positions,
        "params": json.loads(strategy.params),
    }
    risk = get_risk(session)

    _progress.running = True
    _progress.phase = "starting"
    _progress.strategy_name = strategy.name
    _progress.started_at = datetime.now(timezone.utc).isoformat()
    _progress.finished_at = None
    _progress.combos_done = 0
    _progress.combos_total = body.iterations
    _progress.error = None
    _progress.result = None
    _task = asyncio.create_task(
        _run_search(
            client, strategy_dict, risk, symbols, strategy.asset_class,
            timeframe, body.days, body.iterations, body.starting_cash, body.spread_pct,
            prebuilt_bars=prebuilt_bars, eligible_by_day=eligible_by_day, replay_extra=replay_extra,
        )
    )
    return {
        "ok": True, "started": True, "symbols": symbols, "iterations": body.iterations,
        "scanner_replay": body.scanner_replay,
    }


@router.get("/status")
def optimize_status() -> dict:
    return asdict(_progress)
