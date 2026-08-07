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
from qt.timeutil import iso_utc
from qt.api.backtest import refuse_if_dca, replay_strategy

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
    # How far apart the values tried for each setting are, as a fraction of the
    # setting itself: 0.15 = each step is 15% up or down from the last. Bounded
    # rather than free-form — below ~5% the steps are smaller than the noise in a
    # few months of history, and above 50% neighbouring values are different
    # strategies rather than a refinement of one.
    relative_step: float = Field(default=0.15, ge=0.05, le=0.5)


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


def optimizer_lineage(session: Session, strategy: Strategy, days: int) -> dict:
    """How many times this strategy's ancestry has already been through a
    parameter search, and over what windows.

    Why this exists: the out-of-sample split is the optimizer's one real defence,
    and it is a ONCE-PER-SLICE resource. Search a strategy, save the draft, search
    THAT draft over the same history, and the held-out portion is no longer held
    out — the person at the keyboard is now choosing configurations knowing how
    they scored on it. After a few rounds that slice has effectively seen
    thousands of configs while the app still prints a confident number.

    Note what is NOT counted as fresh data: every window ends today, so a shorter
    one is a SUBSET of a longer one. Changing 180 days to 200 does not give the
    search history it hasn't already picked against — only new symbols, or the
    passage of time, does that. Hence `windows`, so the UI can say so rather than
    implying a different day count resets anything."""
    chain: list[dict] = []
    seen: set[int] = set()
    current: Strategy | None = strategy
    while current is not None and current.optimized_from_id is not None:
        if current.id in seen:
            break  # defensive: a cycle would otherwise loop forever
        seen.add(current.id)
        chain.append(
            {
                "days": current.optimized_days,
                "at": iso_utc(current.optimized_at),
            }
        )
        current = session.get(Strategy, current.optimized_from_id)
    return {
        # The generation this run WILL produce: a hand-built strategy yields 1.
        "generation": len(chain) + 1,
        "windows": [c["days"] for c in chain if c["days"]],
        "same_window": sum(1 for c in chain if c["days"] == days),
        # The strategy the whole line descends from, if it still exists.
        "root": current.name if current is not None and current is not strategy else None,
    }


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
    relative_step: float = optimizer.RELATIVE_STEP_DEFAULT,
    prebuilt_bars: dict | None = None,
    prebuilt_daily: dict | None = None,
    eligible_by_day: dict | None = None,
    replay_extra: dict | None = None,
    replay_ctx: dict | None = None,
    lineage: dict | None = None,
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
    from qt.api.backtest import BASELINE_WARMUP_DAYS, DEFAULT_FEE_PCT, warmup_days_for
    from qt.services.backtest import (
        DAILY_RANK_METRICS,
        RANK_LOOKBACK_DAYS,
        ranking_config,
    )

    sim_start: datetime | None = None
    daily_bars: dict[str, list[dict]] | None = None
    # A ranked strategy scored on a DAILY-BAR metric needs its own (deeper) daily
    # history — relative_strength is a 200-day average, so live fetches 320 days.
    # Scanner-replay strategies never rank (the scanner ordered them already), so
    # this stays 0 down that branch and nothing changes there.
    rank_cfg = ranking_config(strategy_dict)
    rank_daily: dict[str, list[dict]] | None = None
    rank_lookback = (
        RANK_LOOKBACK_DAYS[rank_cfg[0]] + 5
        if rank_cfg is not None and rank_cfg[0] in DAILY_RANK_METRICS
        else 0
    )
    try:
        if prebuilt_bars is not None:
            bars = prebuilt_bars
            # Scanner replay: the daily series read from the same cache, used as
            # the INDICATOR source only (never split, never the replay timeline).
            daily_bars = prebuilt_daily
            if replay_ctx:
                # Top up the intraday cache if this strategy needs bars the cache
                # is short of, then recompute everything from the reloaded
                # dataset — the search must describe what it ACTUALLY replayed,
                # and after a successful top-up that is a different (intraday,
                # possibly mixed) dataset than the request handler saw.
                from qt.api.backtest import (
                    ensure_replay_intraday,
                    fetch_held_position_bars,
                    load_scanner_replay_dataset,
                    replay_inputs,
                )
                from qt.services.backtest import run_backtest

                _progress.phase = "downloading 15-minute bars"

                def _report(msg: str, _pct: int | None = None) -> None:
                    _progress.phase = msg

                # Bound BEFORE the top-up: the held-position pass below reads it
                # whether or not anything was downloaded, and leaving it assigned
                # only inside the `if topped_up` branch made every search with an
                # already-complete cache die on an unbound local.
                ds = replay_ctx["ds"]
                ds2, topped_up = await ensure_replay_intraday(
                    client, replay_ctx["ds"], strategy_dict["params"],
                    asset_class=replay_ctx["asset_class"], days=replay_ctx["days"],
                    replay_top_n=replay_ctx["replay_top_n"],
                    scanner_cfg=replay_ctx["scanner_cfg"], report=_report,
                    always_eligible=replay_ctx.get("pinned"),
                )
                if topped_up:
                    fresh = replay_inputs(
                        ds2, strategy_dict["params"], replay_ctx["replay_top_n"]
                    )
                    bars = fresh["bars"]
                    daily_bars = fresh["daily"]
                    eligible_by_day = fresh["eligible_by_day"]
                    timeframe = fresh["timeframe"]
                    mixed = fresh["mixed"]
                    if replay_extra is not None:
                        replay_extra.update(fresh["extra"])
                        replay_extra["intraday_topped_up"] = True
                    ds = ds2

                # HELD-POSITION COVERAGE. The cache holds 15-minute bars only
                # around each symbol's mover-days, so a position held after its
                # symbol left the top-N list falls back to daily bars — which can
                # resolve an exit just once a day, tying up the slot and the cash
                # until then. The backtest fixes this by replaying once to learn
                # its holdings and fetching exactly those days.
                #
                # A search can't do that per config: it runs hundreds, each
                # holding different names for different spans, and fetching for
                # every one would download far more than the search uses. It uses
                # the BASE strategy's holdings instead — the anchor every grid is
                # built around, so the configs tried are all near it and their
                # holdings overlap heavily. Imperfect on purpose, and far better
                # than searching entirely on daily-filled days.
                if ds.used_intraday and ds.daily_filled_days:
                    _progress.phase = "downloading bars for held positions"
                    probe = await asyncio.to_thread(
                        run_backtest,
                        strategy_dict, bars, risk,
                        starting_cash=starting_cash, spread_pct=spread_pct,
                        # Charged here too: this probe decides which held days get
                        # real intraday bars, and a fee-free probe holds a
                        # different set of positions than the search then scores.
                        fee_pct=DEFAULT_FEE_PCT.get(asset_class, 0.0),
                        market=("crypto" if asset_class == "crypto" else "stock"),
                        eligible_by_day=eligible_by_day,
                        daily_bars_by_symbol=daily_bars,
                    )
                    if "error" not in probe and await fetch_held_position_bars(
                        client, probe, ds, asset_class=asset_class, report=_report
                    ):
                        ds = await asyncio.to_thread(
                            load_scanner_replay_dataset,
                            replay_ctx["asset_class"], replay_ctx["days"],
                            replay_ctx["replay_top_n"], replay_ctx["scanner_cfg"],
                            always_eligible=replay_ctx.get("pinned"),
                        )
                        fresh = replay_inputs(
                            ds, strategy_dict["params"], replay_ctx["replay_top_n"]
                        )
                        bars = fresh["bars"]
                        daily_bars = fresh["daily"]
                        eligible_by_day = fresh["eligible_by_day"]
                        timeframe = fresh["timeframe"]
                        mixed = fresh["mixed"]
                        if replay_extra is not None:
                            replay_extra.update(fresh["extra"])
                            replay_extra["intraday_topped_up"] = True

                # WHERE TRADING MAY BEGIN. The dataset now carries a BASELINE
                # prefix in front of the window — bars that exist only to give the
                # window's first bar a day-gain reference — and this path never
                # gated it, so the search TRADED those days: the window it
                # reported was not the window it searched, and the in/out
                # boundary (measured from `sim_start`) sat inside the prefix, so
                # the 70/30 split was neither. `_scanner_replay` has always
                # floored at `ds.start_day`; this is the same floor.
                sim_start = datetime.strptime(ds.start_day, "%Y-%m-%d").replace(
                    tzinfo=timezone.utc
                )
        else:
            # Read-through the bar cache: only the missing recent edge is actually
            # downloaded, and any cache trouble degrades to a plain fetch.
            _progress.phase = "downloading bars"
            window_start = datetime.now(timezone.utc) - timedelta(days=days)
            # TWO warm-ups, because they answer two different questions — the same
            # split qt.api.backtest.replay() makes, which this path never got.
            #
            # The DAILY series needs enough history for THIS strategy's own
            # indicators (warmup_days_for delegates to the live engine's
            # _daily_lookback_days, so a 50-period ATR gets its 110 days instead
            # of one flat constant that was either wasteful or short).
            #
            # The REPLAYED series needs only enough to give its first bar a
            # day-gain baseline — but it needs that ALWAYS, indicators or not.
            # Without it `_prepare` leaves change_pct None on the window's first
            # day and `run_backtest` skips those bars in silence, so every search
            # was blind on day one of its window (and the previous flat
            # `WARMUP_DAYS if daily-signalled else 0` gave a plain intraday search
            # no prefix at all).
            warmup = warmup_days_for(strategy_dict["params"], asset_class)
            # The ranking metric's lookback is a claim on the SAME prefix, and a
            # deeper one than any indicator. Fetch short and the metric is None
            # across the opening stretch, every member drops out of the ranking,
            # and the search scores every config at zero trades.
            warmup = max(warmup, rank_lookback)
            baseline_warmup = BASELINE_WARMUP_DAYS.get(asset_class, 5)
            start = (window_start - timedelta(days=warmup)).strftime("%Y-%m-%dT%H:%M:%SZ")
            baseline_start = (
                window_start - timedelta(days=baseline_warmup)
            ).strftime("%Y-%m-%dT%H:%M:%SZ")
            if mixed:
                # Two fetches with deliberately different windows, exactly like the
                # mixed backtest: the DAILY series reaches back over the warm-up so
                # the indicators are defined from day one, while the 15-minute
                # series covers only the tested window (96 bars/symbol/day for
                # crypto — fetching warm-up intraday too would multiply the
                # download for bars that could never trade).
                daily_bars = await barfetch.fetch_bars(client, symbols, asset_class, "1Day", start)
                # From `baseline_start`, not `window_start`: the mixed branch used
                # to open its 15-minute series exactly at the window, so the first
                # day had no day-gain reference and was silently unusable.
                bars = await barfetch.fetch_bars(
                    client, symbols, asset_class, "15Min", baseline_start,
                )
            else:
                # A 1Day search reads its own bars as the indicator source, so it
                # needs the deep prefix; an intraday one only needs the baseline
                # days (15-minute crypto bars run 96 per symbol per day, so
                # pulling the indicator warm-up at that size would be an enormous
                # download for bars that can never trade).
                bars = await barfetch.fetch_bars(
                    client, symbols, asset_class, timeframe,
                    start if timeframe == "1Day" else baseline_start,
                )
            if rank_lookback:
                # A 1Day search IS a daily series (run_backtest reads its own bars
                # then) and a mixed one already has one; the remaining case is an
                # intraday search of a strategy with a daily RANKING metric but no
                # daily indicator. SPY is only ever fetched for rs_vs_spy.
                if mixed:
                    rank_daily = daily_bars
                elif timeframe != "1Day":
                    rank_daily = await barfetch.fetch_bars(
                        client, symbols, asset_class, "1Day", start
                    )
                if rank_cfg[0] == "rs_vs_spy":
                    spy = await barfetch.fetch_bars(client, ["SPY"], "stock", "1Day", start)
                    rank_daily = {**(rank_daily or bars), "SPY": spy.get("SPY") or []}
            if not any(bars.get(s) for s in symbols):
                raise ValueError("No historical bars for those symbols/timeframe.")
            # ALWAYS, now that every branch fetches a prefix. It is what stops the
            # baseline days being traded, and it is what tells
            # split_in_out_of_sample to measure the in/out boundary over the
            # WINDOW rather than over window+warm-up — with no sim_start the
            # boundary sat inside the warm-up prefix and the "70/30" split was not
            # 70/30 at all.
            sim_start = window_start

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
            # THE SAME COMMISSION THE BACKTEST PAGE CHARGES. Without it the search
            # scored every config fee-free while both single-strategy backtest
            # paths charged crypto 0.25% a side — so the optimizer preferred
            # busier settings and the Backtest page then scored its winner worse
            # on the identical window. The two tools now cost a trade the same.
            fee_pct=DEFAULT_FEE_PCT.get(asset_class, 0.0),
            relative_step=relative_step,
            market=market,
            eligible_by_day=eligible_by_day,
            progress=on_progress,
            sim_start=sim_start,
            daily_bars_by_symbol=daily_bars,
            rank_daily_bars_by_symbol=rank_daily,
        )
        result["strategy_name"] = _progress.strategy_name
        # `timeframe` is what was REPLAYED (what the searched stops were checked
        # on); on a mixed run the signals came from a coarser series, named apart.
        result["timeframe"] = timeframe
        result["mixed_resolution"] = mixed
        if mixed:
            result["signal_timeframe"] = "1Day"
        result["days"] = days
        # How many times this strategy's line has already been searched. Carried
        # on the RESULT rather than only shown at start time, because it is the
        # number that qualifies the out-of-sample figure printed beside it.
        result["lineage"] = lineage
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
    # A search over rules the live engine never reads would be worse than a
    # single wrong backtest — it would tune them. Same guard, same reason.
    refuse_if_dca(replay_strategy(strategy).get("params") or {})

    # Scanner-replay mode reads its universe (each past day's top-N risers) +
    # their bars OFFLINE from the bar cache; the fixed-universe mode resolves a
    # symbol list and downloads bars from Alpaca in the background task.
    prebuilt_bars: dict | None = None
    prebuilt_daily: dict | None = None
    eligible_by_day: dict | None = None
    replay_ctx: dict | None = None
    replay_extra: dict | None = None
    timeframe = body.timeframe
    mixed = False
    # Derived from the strategy, not taken from the request: a scanner strategy
    # IS its day-varying universe, and nothing else can meaningfully opt in or
    # out of that. body.scanner_replay / body.replay_top_n are ignored.
    # "both" is scanner AND watchlist: replayed against the risers, with the
    # watchlist pinned as eligible every day. Searching it as watchlist-only
    # tuned the settings against half the universe the engine really trades.
    scanner_replay = strategy.universe in ("scanner", "both")
    replay_top_n = strategy.top_n or 10
    if scanner_replay:
        from qt.api.backtest import load_scanner_replay_dataset, replay_inputs

        from qt.services import scanner as scanner_svc

        # Same rule as the backtest: search the universe the engine would really
        # trade, not one frozen at sweep time.
        scanner_cfg = scanner_svc.get_config(session)
        pinned = _resolve_symbols(session, strategy) if strategy.universe == "both" else []
        ds = load_scanner_replay_dataset(
            strategy.asset_class, body.days, replay_top_n, scanner_cfg, always_eligible=pinned,
        )
        # Derived here only to validate the request and answer it promptly. If the
        # intraday cache is short, the background task tops it up and recomputes
        # ALL of this from the reloaded dataset — the download is far too slow to
        # hold a request open for (Cloudflare cuts the origin off at 100s).
        inputs = replay_inputs(ds, json.loads(strategy.params), replay_top_n)
        prebuilt_bars = inputs["bars"]
        prebuilt_daily = inputs["daily"]
        eligible_by_day = inputs["eligible_by_day"]
        timeframe = inputs["timeframe"]
        mixed = inputs["mixed"]
        replay_extra = inputs["extra"]
        # Names that made a top-N list AND actually have bars (offline: no 25 cap).
        # Searching over ds.union would hand the optimizer symbols with no data —
        # silently dropped downstream, exactly the way the backtest used to drop
        # them — while every count here overstated what was really tested. The
        # backtest was fixed to report what it REPLAYED; this is the other consumer
        # of the same dataset and has to agree.
        symbols = ds.replayed
        # What the task needs to re-read the dataset for itself after a top-up.
        replay_ctx = {
            "ds": ds,  # same in-process objects already passed as prebuilt_bars
            "asset_class": strategy.asset_class,
            "days": body.days,
            "replay_top_n": replay_top_n,
            "scanner_cfg": scanner_cfg,
            "pinned": pinned,
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
        from qt.api.backtest import (
            _mixed_resolution,
            _uses_daily_only_signals,
            daily_signal_names,
        )

        params = json.loads(strategy.params)
        mixed = _mixed_resolution(params)
        if body.timeframe in ("15Min", "1Hour") and _uses_daily_only_signals(params) and not mixed:
            raise HTTPException(
                status_code=422,
                detail=f"This strategy uses {daily_signal_names(params)}, which the live engine "
                "reads off completed DAILY bars — an intraday search would measure it over hours "
                "instead of days and wouldn't match live. Use 1 Day.",
            )
        if mixed:
            # A property of the STRATEGY, not of what was asked for: the replay is
            # 15-minute whatever timeframe the caller sent.
            timeframe = "15Min"

    # Read everything the (session-less) background task needs NOW, while the
    # request's DB session is open — pass plain dicts/lists into the task.
    strategy_dict = {
        "asset_class": strategy.asset_class,
        # The universe cut the LIVE engine makes every cycle. Without these the
        # search evaluated a ranked strategy's whole pool while live evaluates only
        # its top-N — so every config it scored had more candidates to work with
        # than it would have had in reality, and the search was tuned against a
        # more generous world than the one it was tuning for.
        "universe": strategy.universe,
        "rank_enabled": strategy.rank_enabled,
        "rank_by": strategy.rank_by,
        "top_n": strategy.top_n,
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
            relative_step=body.relative_step,
            prebuilt_bars=prebuilt_bars, prebuilt_daily=prebuilt_daily,
            eligible_by_day=eligible_by_day, replay_extra=replay_extra,
            replay_ctx=replay_ctx,
            lineage=optimizer_lineage(session, strategy, body.days),
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
    # Which strategy is being swept — the status view has to name it, or a run
    # left going overnight is a table of numbers with no idea what produced them.
    strategy_name: str = ""
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
    # WHICH strategy to sweep. The point of the feature is testing one config
    # against every basket without editing its universe fifteen times, so the
    # strategy is the input, not a fixed internal template.
    strategy_id: int
    days: int = Field(default=365, ge=90, le=730)
    iterations: int = Field(default=60, ge=5, le=200)  # per basket
    spread_pct: float = Field(default=0.1, ge=0, le=2)


async def _run_sweep(
    client: AlpacaClient, baskets: list[dict], risk: dict,
    days: int, iterations: int, spread_pct: float, strategy: dict,
) -> None:
    """Background worker: ONE batched daily-bar download for the union of every
    basket's symbols (+ SPY for the margin), then the whole sweep in a worker
    thread.

    The download window is EXTENDED by the swept strategy's own warm-up: a
    strategy using MACD, RSI or ATR needs history before the window to have a
    defined signal on day one, and without it every basket would be scored on a
    strategy that was blind for its first ~35 bars. The old fixed template used
    no daily indicators, so this did not arise."""
    try:
        _sweep_progress.phase = "downloading bars"
        from qt.api.backtest import warmup_days_for

        warmup = warmup_days_for(strategy["params"], strategy.get("asset_class", "stock"))
        start = (
            datetime.now(timezone.utc) - timedelta(days=days + warmup)
        ).strftime("%Y-%m-%dT%H:%M:%SZ")
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
            strategy=strategy,
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

    # The STRATEGY is validated before the environment. Refusing a DCA sleeve
    # only when baskets happen to exist would make the guard depend on unrelated
    # account state — and it hid the check from its own test, which is how the
    # gap surfaced.
    strategy = session.get(Strategy, body.strategy_id)
    if strategy is None:
        raise HTTPException(status_code=404, detail="Strategy not found.")
    refuse_if_dca(replay_strategy(strategy).get("params") or {})

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

    # Same shape the single search builds — including the ranking cut, which is a
    # real part of the strategy and must be scored, not silently dropped.
    strategy_dict = {
        "name": strategy.name,
        "asset_class": strategy.asset_class,
        "universe": strategy.universe,
        "rank_enabled": strategy.rank_enabled,
        "rank_by": strategy.rank_by,
        "top_n": strategy.top_n,
        "swing_mode": strategy.swing_mode,
        "sizing_usd": strategy.sizing_usd,
        "sleeve_usd": strategy.sleeve_usd,
        "max_positions": strategy.max_positions,
        "params": json.loads(strategy.params),
    }

    risk = get_risk(session)
    _sweep_progress.running = True
    _sweep_progress.phase = "starting"
    _sweep_progress.strategy_name = strategy.name
    _sweep_progress.baskets_total = len(baskets)
    _sweep_progress.baskets_done = 0
    _sweep_progress.current_basket = ""
    _sweep_progress.started_at = datetime.now(timezone.utc).isoformat()
    _sweep_progress.finished_at = None
    _sweep_progress.error = None
    _sweep_progress.result = None
    _sweep_task = asyncio.create_task(
        _run_sweep(
            client, baskets, risk, body.days, body.iterations, body.spread_pct,
            strategy_dict,
        )
    )
    return {"ok": True, "started": True, "baskets": len(baskets), "iterations": body.iterations}


@router.get("/sweep/status")
def sweep_status() -> dict:
    return asdict(_sweep_progress)
