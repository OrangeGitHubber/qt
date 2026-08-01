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
from qt.models import Basket, BasketItem, Strategy, WatchlistItem
from qt.services import barfetch, optimizer, sweep
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
    # IGNORED. The universe is the strategy's own — see _resolve_symbols. Kept on
    # the model so an older frontend posting them still works rather than 422ing.
    symbols: list[str] = []
    scanner_replay: bool = False
    replay_top_n: int = Field(default=10, ge=1, le=100)  # how many of each day's risers are eligible
    days: int = Field(default=180, ge=30, le=730)
    timeframe: str = Field(default="1Day", pattern="^(15Min|1Hour|1Day)$")
    iterations: int = Field(default=40, ge=5, le=200)
    starting_cash: float = Field(default=5000, ge=100, le=10_000_000)
    spread_pct: float = Field(default=0.1, ge=0, le=2)


def _resolve_symbols(session: Session, strategy: Strategy) -> list[str]:
    """The symbols this search runs on — resolved from the STRATEGY, never from
    the request.

    Callers used to be able to substitute their own list. Tuning a basket
    rotator against three hand-picked names produces settings fitted to those
    three, then hands them to a strategy that trades a different pool: a result
    describing an experiment you can never actually run, and the exact shape of
    overfitting the out-of-sample split exists to catch. The backtest was locked
    to the strategy's universe for the same reason; this is the other half.

    A custom list as-is, a basket's members, or the asset-class watchlist. A
    scanner strategy is replayed against its real day-varying universe elsewhere
    (see scanner_replay); the watchlist here is its fallback."""
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
    mixed: bool = False,
) -> None:
    """Background worker: get the bars once, then run the search in a worker
    thread (it is CPU-heavy — dozens of full backtests — so it must not block the
    event loop) with a progress callback the status endpoint reads.

    Fixed-universe mode downloads the bars from Alpaca. Scanner-replay mode
    passes `prebuilt_bars` (read offline from the cache) + `eligible_by_day` (each
    day's top-N risers) so every backtest can only ENTER a symbol on the days it
    actually rose — the search then optimizes the strategy against its real
    universe, not a stand-in watchlist.

    `mixed` = MIXED-RESOLUTION search (see qt.api.backtest._mixed_resolution): two
    fetches, a daily series for the indicators and 15-minute bars for the replay,
    so the searched stops are tested against real intraday prices."""
    # Warm-up: a daily MACD/RSI/ATR search needs history BEFORE the window so the
    # indicators are defined from the first traded bar — in BOTH the in-sample and
    # out-of-sample slices (the split gives each its own warm-up prefix). Without
    # it the out-of-sample slice — the honest verdict number — begins mid-window
    # with the signal dead for its first ~35 bars. Daily-only, same as the backtest.
    from qt.api.backtest import WARMUP_DAYS, _needs_warmup

    sim_start: datetime | None = None
    daily_bars: dict[str, list[dict]] | None = None
    try:
        if prebuilt_bars is not None:
            bars = prebuilt_bars
        else:
            # Read-through the bar cache: only the missing recent edge is actually
            # downloaded, and any cache trouble degrades to a plain fetch.
            _progress.phase = "downloading bars"
            window_start = datetime.now(timezone.utc) - timedelta(days=days)
            needs_warmup = _needs_warmup(strategy_dict["params"])
            warmup = WARMUP_DAYS if (timeframe == "1Day" or mixed) and needs_warmup else 0
            start = (window_start - timedelta(days=warmup)).strftime("%Y-%m-%dT%H:%M:%SZ")
            if mixed:
                # Two fetches with deliberately different windows, exactly like the
                # mixed backtest: the DAILY series reaches back over the warm-up so
                # the indicators are defined from day one, while the 15-minute
                # series covers only the tested window (96 bars/symbol/day for
                # crypto — fetching warm-up intraday too would multiply the
                # download for bars that could never trade).
                daily_bars = await barfetch.fetch_bars(client, symbols, asset_class, "1Day", start)
                bars = await barfetch.fetch_bars(
                    client, symbols, asset_class, "15Min",
                    window_start.strftime("%Y-%m-%dT%H:%M:%SZ"),
                )
            else:
                bars = await barfetch.fetch_bars(client, symbols, asset_class, timeframe, start)
            if not any(bars.get(s) for s in symbols):
                raise ValueError("No historical bars for those symbols/timeframe.")
            # A mixed run always gates trading at the window start: its intraday
            # bars begin there, and the daily series before it is warm-up only.
            sim_start = window_start if (warmup or mixed) else None

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
            sim_start=sim_start,
            daily_bars_by_symbol=daily_bars,
        )
        result["strategy_name"] = _progress.strategy_name
        # `timeframe` is what was REPLAYED (what the searched stops were checked
        # on); on a mixed run the signals came from a coarser series, named apart.
        result["timeframe"] = timeframe
        result["mixed_resolution"] = mixed
        if mixed:
            result["signal_timeframe"] = "1Day"
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
    if _sweep_progress.running:
        raise HTTPException(status_code=409, detail="A basket sweep is already running — wait for it to finish.")

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
    mixed = False
    # Derived from the strategy, not taken from the request: a scanner strategy
    # IS its day-varying universe, and nothing else can meaningfully opt in or
    # out of that. body.scanner_replay / body.replay_top_n are ignored.
    scanner_replay = strategy.universe == "scanner"
    replay_top_n = strategy.top_n or 10
    if scanner_replay:
        from qt.api.backtest import load_scanner_replay_dataset

        ds = load_scanner_replay_dataset(strategy.asset_class, body.days, replay_top_n)
        prebuilt_bars = ds.bars
        eligible_by_day = ds.eligible_by_day
        timeframe = ds.timeframe  # 15Min if intraday cached, else 1Day — from the cache
        # Names that made a top-N list AND actually have bars (offline: no 25 cap).
        # Searching over ds.union would hand the optimizer symbols with no data —
        # silently dropped downstream, exactly the way the backtest used to drop
        # them — while every count here overstated what was really tested. The
        # backtest was fixed to report what it REPLAYED; this is the other consumer
        # of the same dataset and has to agree.
        symbols = ds.replayed
        replay_extra = {
            "scanner_replay": True,
            "replay_intraday": ds.used_intraday,
            "replay_top_n": replay_top_n,
            "universe_size": len(ds.replayed),  # what was TESTED, not what made a list
            "universe_dropped": ds.dropped,
            "intraday_covered": ds.intraday_covered,
            "days_replayed": ds.days_replayed,
        }
    else:
        symbols = _resolve_symbols(session, strategy)
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
        # The mirror of the above: MACD/RSI are DAILY signals live, so an intraday
        # search computes a twitchy intraday MACD/RSI that whipsaws and won't match
        # the live engine — lock the search to 1 Day (same as the backtest).
        #
        # UNLESS the strategy is MIXED RESOLUTION (daily signals AND a price-
        # triggered exit). Three of the four searched knobs — trailing stop,
        # stop-loss, take-profit — are price-triggered exits, and a once-a-day
        # replay checks them only at the close, so a tight stop looks nearly free
        # and the search drifts toward stops that would whipsaw for real. Such a
        # strategy replays 15-minute bars with MACD/RSI still taken from completed
        # daily closes — the same deal the backtest gives it.
        from qt.api.backtest import _mixed_resolution, _uses_daily_only_signals

        params = json.loads(strategy.params)
        mixed = _mixed_resolution(params)
        if body.timeframe in ("15Min", "1Hour") and _uses_daily_only_signals(params) and not mixed:
            raise HTTPException(
                status_code=422,
                detail="This strategy uses MACD/RSI, which are daily signals — an intraday search "
                "whipsaws and won't match the live engine. Use 1 Day.",
            )
        if mixed:
            # A property of the STRATEGY, not of what was asked for: the replay is
            # 15-minute whatever timeframe the caller sent.
            timeframe = "15Min"

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
            mixed=mixed,
        )
    )
    return {
        "ok": True, "started": True, "symbols": symbols, "iterations": body.iterations,
        "scanner_replay": scanner_replay, "timeframe": timeframe,
        "mixed_resolution": mixed,
    }


@router.get("/status")
def optimize_status() -> dict:
    return asdict(_progress)


# ---------------------------------------------------------------------------
# Basket sweep: the parameter search across EVERY basket, ranked by the
# out-of-sample margin over SPY. Same background-task + status shape as the
# single search above; the two are mutually exclusive (both are CPU-heavy).
# ---------------------------------------------------------------------------


@dataclass
class SweepProgress:
    running: bool = False
    phase: str = ""  # "downloading bars" | "searching" | "done"
    baskets_total: int = 0
    baskets_done: int = 0
    current_basket: str = ""
    started_at: str | None = None
    finished_at: str | None = None
    error: str | None = None
    result: dict | None = field(default=None)


_sweep_progress = SweepProgress()
_sweep_task: asyncio.Task | None = None

# Per-basket symbol cap — matches the backtest's basket cap; keeps a mega-basket
# from dominating the one batched bar download.
SWEEP_BASKET_CAP = 25


class SweepBody(BaseModel):
    days: int = Field(default=365, ge=90, le=730)
    iterations: int = Field(default=60, ge=5, le=200)  # per basket
    spread_pct: float = Field(default=0.1, ge=0, le=2)


async def _run_sweep(
    client: AlpacaClient, baskets: list[dict], risk: dict,
    days: int, iterations: int, spread_pct: float,
) -> None:
    """Background worker: ONE batched daily-bar download for the union of every
    basket's symbols (+ SPY for the margin), then the whole sweep in a worker
    thread. Plain-momentum template → no indicator warm-up needed."""
    try:
        _sweep_progress.phase = "downloading bars"
        start = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
        union = sorted({sym for b in baskets for sym in b["symbols"]} | {"SPY"})
        bars: dict[str, list] = {}
        for i in range(0, len(union), 50):  # same 50-symbol batch cap as the search
            bars.update(await client.historical_bars(union[i : i + 50], "stock", "1Day", start))

        _sweep_progress.phase = "searching"

        def on_progress(done: int, total: int, name: str) -> None:
            _sweep_progress.baskets_done = done
            _sweep_progress.baskets_total = total
            _sweep_progress.current_basket = name

        result = await asyncio.to_thread(
            sweep.sweep_baskets, baskets, bars, risk,
            iterations=iterations, spread_pct=spread_pct, progress=on_progress,
        )
        result["days"] = days
        _sweep_progress.result = result
        _sweep_progress.phase = "done"
    except AlpacaError as exc:
        log.exception("basket sweep bar download failed")
        _sweep_progress.error = f"Bar download failed ({exc.status_code}): {exc}"
    except Exception as exc:  # noqa: BLE001 — record any failure for the status view
        log.exception("basket sweep failed")
        _sweep_progress.error = str(exc)
    finally:
        _sweep_progress.running = False
        _sweep_progress.finished_at = datetime.now(timezone.utc).isoformat()


@router.post("/sweep")
async def start_sweep(
    body: SweepBody,
    session: Session = Depends(get_session),
    client: AlpacaClient = Depends(require_client),
) -> dict:
    """Run the parameter search across every basket (stock members only) and rank
    the winners by out-of-sample margin over SPY. Background task; poll
    /sweep/status. Mutually exclusive with the single search."""
    global _sweep_task
    if _sweep_progress.running:
        raise HTTPException(status_code=409, detail="A basket sweep is already running.")
    if _progress.running:
        raise HTTPException(status_code=409, detail="A parameter search is already running — wait for it to finish.")

    baskets: list[dict] = []
    for b in session.query(Basket).order_by(Basket.name).all():
        symbols = sorted(
            {
                i.symbol
                for i in session.query(BasketItem).filter(
                    BasketItem.basket_id == b.id, BasketItem.asset_class == "stock"
                )
            }
        )[:SWEEP_BASKET_CAP]
        if len(symbols) >= 2:
            baskets.append({"id": b.id, "name": b.name, "symbols": symbols})
    if not baskets:
        raise HTTPException(status_code=422, detail="No baskets with at least 2 stock symbols to sweep.")

    risk = get_risk(session)
    _sweep_progress.running = True
    _sweep_progress.phase = "starting"
    _sweep_progress.baskets_total = len(baskets)
    _sweep_progress.baskets_done = 0
    _sweep_progress.current_basket = ""
    _sweep_progress.started_at = datetime.now(timezone.utc).isoformat()
    _sweep_progress.finished_at = None
    _sweep_progress.error = None
    _sweep_progress.result = None
    _sweep_task = asyncio.create_task(
        _run_sweep(client, baskets, risk, body.days, body.iterations, body.spread_pct)
    )
    return {"ok": True, "started": True, "baskets": len(baskets), "iterations": body.iterations}


@router.get("/sweep/status")
def sweep_status() -> dict:
    return asdict(_sweep_progress)
