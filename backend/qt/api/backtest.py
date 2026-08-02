"""Backtest endpoint: replay a saved strategy over history.

Two ways in, same work:

* ``POST /api/backtest`` (and ``/portfolio``) run the replay INSIDE the request
  and return the result. Simple, and what the tests drive.
* ``POST /api/backtest/start`` runs the very same handler as a background task
  and returns a job id to poll at ``/api/backtest/job/{id}``.

The second exists because a long replay outlives an HTTP request. A 350-day,
30-symbol backtest takes minutes, and anything in front of this container gives
up long before that: nginx defaults to a 60-second read timeout and Cloudflare
enforces a fixed 100 seconds (HTTP 524) that no plan setting can raise. The
work was fine — the connection died. Polling a job keeps every request short,
so the proxy has nothing to time out.
"""

import asyncio
import json
import logging
import uuid
from collections.abc import Callable
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from qt.api.market import require_client
from qt.broker.alpaca import AlpacaClient, AlpacaError
from qt.db import get_session, session_scope
from qt.models import BasketItem, Strategy, WatchlistItem
from qt.services import backtest, barfetch
from qt.services.engine import get_risk

log = logging.getLogger("qt.api.backtest")

router = APIRouter(prefix="/api/backtest", tags=["backtest"])

# Where a running backtest says what it's up to. A ContextVar rather than an
# argument because the handlers below are FastAPI routes: an extra parameter
# would be read as a request field. Background jobs install a sink here; a direct
# POST leaves it unset and _report() costs nothing. asyncio.to_thread copies the
# context, so reports from inside the (threaded) replay arrive too.
_reporter: ContextVar[Callable[[str, int | None], None] | None] = ContextVar("bt_reporter", default=None)


def _report(phase: str, pct: int | None = None) -> None:
    sink = _reporter.get()
    if sink is not None:
        sink(phase, pct)


def _replay_progress(done: int, total: int) -> None:
    """run_backtest's observer hook — bars replayed out of bars total."""
    _report("Replaying history…", int(done * 100 / total) if total else 0)


TIMEFRAMES = ("15Min", "1Hour", "1Day")

# Commission per side, as a % of notional. US equities on Alpaca are
# commission-free (a sell carries tiny SEC/TAF regulatory fees — cents, and not
# worth modelling). CRYPTO is not free: Alpaca charges a volume-tiered
# 0.15%-0.25% per side at the entry tier (under $100k/month), so a round trip
# costs roughly half a percent. A strategy taking 1-3% moves several times a day
# hands over a serious share of its edge, and a backtest that ignores this
# reports a profit the real account would never have seen.
#
# https://docs.alpaca.markets/docs/crypto-fees
DEFAULT_FEE_PCT = {"stock": 0.0, "crypto": 0.25}


@dataclass
class ScannerReplayDataset:
    """The offline scanner-replay dataset read from the bar cache: for each past
    day, only that day's cached top-N movers are eligible to enter. Shared by the
    single-strategy scanner-replay backtest and the scanner-replay optimizer so
    both build the universe the exact same way."""
    bars: dict[str, list[dict]]
    eligible_by_day: dict[str, set[str]]
    timeframe: str
    used_intraday: bool
    union: list[str]
    market: str
    benchmark_class: str
    benchmark_symbol: str
    start_day: str
    days_replayed: int
    # Symbols actually replayed vs. those that made a mover list but had no bars
    # at the chosen resolution. `universe_size` must report what was TESTED, not
    # what was hoped for — a silently shrunken universe is a wrong result that
    # looks right.
    replayed: list[str]
    dropped: list[str]
    intraday_covered: int


def load_scanner_replay_dataset(
    asset_class: str, days: int, replay_top_n: int, scanner_cfg: dict | None = None
) -> ScannerReplayDataset:
    """Read the cached historical top-N risers + their bars for `days` back.
    Prefers cached INTRADAY bars (so intraday exits behave); falls back to daily
    when no intraday sweep has been run. Fully offline. Raises HTTPException 422
    when the cache is empty and 502 on a bad cache DSN — both API-shaped because
    both callers are FastAPI routes."""
    from qt.services import barcache

    crypto = asset_class == "crypto"
    daily_model = barcache.CryptoDailyBar if crypto else barcache.DailyBar
    mover_model = barcache.CryptoDailyMover if crypto else barcache.DailyMover
    intraday_model = barcache.CryptoIntradayBar if crypto else barcache.IntradayBar
    daily_stamp = "T12:00:00Z" if crypto else "T14:00:00Z"
    market = "crypto" if crypto else "stock"
    market_key = "crypto" if crypto else "stocks"  # the scanner config's own key
    benchmark_symbol = "BTC/USD" if crypto else "SPY"

    try:
        barcache.init_cache()
    except Exception as exc:  # noqa: BLE001 — surface a bad cache DSN clearly
        raise HTTPException(status_code=502, detail=f"Could not open the bar cache DB: {exc}")

    start_day = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")
    cache = barcache.session()
    try:
        # The replay's universe must obey the SAME scanner settings the live
        # engine obeys. Read them now rather than trusting whatever was in force
        # when the sweep ran — an exclusion added since, or a price floor raised,
        # would otherwise keep feeding the backtest names it must never trade.
        f = scanner_cfg[market_key] if scanner_cfg else {}
        movers = barcache.movers_between(
            cache, start_day, top_n=replay_top_n, model=mover_model,
            exclude=set(scanner_cfg.get("exclude_symbols") or []) if scanner_cfg else set(),
            min_price=float(f.get("min_price") or 0),
            max_price=float(f.get("max_price") or 0),
            min_dollar_volume=float(f.get("min_dollar_volume") or 0),
        )
        if not movers:
            asset = "crypto" if crypto else "stock"
            raise HTTPException(
                status_code=422,
                detail=f"No cached {asset} movers yet — run a {'crypto ' if crypto else ''}sweep first "
                       "(Settings → Historical bar cache).",
            )
        eligible_by_day = {day: set(syms) for day, syms in movers.items()}
        union = sorted({s for syms in movers.values() for s in syms})
        # Intraday needs to cover the WHOLE mover set, not just some of it. This
        # used to test `any(...)`, which was safe only by accident: the intraday
        # table was filled exclusively by the "Sweep intraday" job, which fetches
        # every mover — all-or-nothing. Once ordinary backtests began caching
        # their bars too, a single incidental symbol (a name you happened to
        # backtest that later showed up as a mover) could flip the whole replay
        # to intraday, and every uncovered symbol — silently dropped downstream,
        # because run_backtest skips empty series — would vanish while the header
        # still claimed the full universe. Demand full coverage or use daily,
        # which the daily sweep fills completely.
        intraday_bars = barcache.cached_intraday_bars(cache, union, start_day, model=intraday_model)
        intraday_covered = sorted(s for s in union if intraday_bars.get(s))
        used_intraday = len(intraday_covered) == len(union) and bool(union)
        if used_intraday:
            bars = intraday_bars
            timeframe = "15Min"
        else:
            bars = barcache.cached_daily_bars(cache, union, start_day, model=daily_model, stamp=daily_stamp)
            timeframe = "1Day"
        # Whatever the resolution, a symbol with no bars can't be replayed. Name
        # them rather than quietly shrinking the universe under the user.
        replayed = sorted(s for s in union if bars.get(s))
        dropped = sorted(set(union) - set(replayed))
    finally:
        cache.close()

    return ScannerReplayDataset(
        bars=bars, eligible_by_day=eligible_by_day, timeframe=timeframe,
        used_intraday=used_intraday, union=union, market=market,
        benchmark_class=market, benchmark_symbol=benchmark_symbol,
        start_day=start_day, days_replayed=len(movers),
        replayed=replayed, dropped=dropped, intraday_covered=len(intraday_covered),
    )


class BacktestBody(BaseModel):
    strategy_id: int
    symbols: list[str] = []  # empty = use the watchlist for the strategy's asset class
    scanner_replay: bool = False  # replay the cached historical daily top-N risers instead
    replay_top_n: int = Field(default=10, ge=1, le=100)  # how many of each day's risers are eligible
    days: int = Field(default=90, ge=7, le=730)
    timeframe: str = Field(default="1Hour", pattern="^(15Min|1Hour|1Day)$")
    starting_cash: float = Field(default=5000, ge=100, le=10_000_000)
    spread_pct: float = Field(default=0.1, ge=0, le=2)
    # None = use the asset class's real-world rate (see DEFAULT_FEE_PCT). An
    # explicit 0 is honoured, for asking "what would this look like fee-free?".
    fee_pct: float | None = Field(default=None, ge=0, le=2)


class PortfolioBacktestBody(BaseModel):
    strategy_ids: list[int] = Field(..., min_length=1, max_length=12)
    days: int = Field(default=90, ge=7, le=730)
    timeframe: str = Field(default="1Hour", pattern="^(15Min|1Hour|1Day)$")
    starting_cash: float = Field(default=5000, ge=100, le=10_000_000)
    spread_pct: float = Field(default=0.1, ge=0, le=2)


def _strategy_symbols(session: Session, strategy: Strategy) -> list[str]:
    """Resolve the symbols a strategy trades in a portfolio backtest. A merged
    timeline can't reconstruct the scanner's historical daily picks, so a
    scanner strategy falls back to the watchlist for its asset class — the same
    honest limitation the single-strategy fixed-list mode carries."""
    if strategy.universe == "custom":
        return [s.strip().upper() for s in (json.loads(strategy.symbols) if strategy.symbols else []) if s.strip()]
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
    # scanner | watchlist | both → the watchlist for this asset class
    return sorted(
        {
            i.symbol
            for i in session.query(WatchlistItem).filter(
                WatchlistItem.asset_class == strategy.asset_class
            )
        }
    )


def _uses_daily_only_signals(params: dict) -> bool:
    """MACD or RSI gate on, and NOT the (intraday-only) VWAP rule. The live engine
    computes MACD/RSI from COMPLETED DAILY bars, so an intraday backtest computes
    them on intraday closes — twitchy and unlike live. Such a strategy must be
    backtested on 1Day. (If VWAP is also on, the strategy is misconfigured; the
    VWAP guard — which needs intraday — takes precedence and this one stands down
    so the two don't deadlock.)"""
    entry = params.get("entry", {})
    exit_rules = params.get("exit", {})
    if entry.get("require_above_vwap"):
        return False
    macd = bool(entry.get("require_macd_bullish") or exit_rules.get("exit_on_macd_bearish"))
    rsi = (
        float(entry.get("rsi_min", 0) or 0) > 0
        or float(entry.get("rsi_max", 0) or 0) > 0
        or float(exit_rules.get("exit_rsi_above", 0) or 0) > 0
    )
    return macd or rsi


def _has_price_triggered_exit(params: dict) -> bool:
    """Whether any exit fires off the PRICE itself — stop-loss, trailing stop or
    take-profit. These are the rules a once-a-day daily replay cannot simulate:
    it checks them at the close, so a position that dipped through its stop and
    recovered is scored a winner."""
    x = params.get("exit") or {}
    return any(
        float(x.get(k, 0) or 0) > 0
        for k in ("stop_loss_pct", "trailing_stop_pct", "take_profit_pct")
    )


def _mixed_resolution(params: dict) -> bool:
    """The strategy that needs BOTH resolutions at once: its signals are daily
    (MACD/RSI — the live engine reads them off completed daily closes) but its
    exits are price-triggered (a stop only means something intraday). On one bar
    stream it can have correct signals with fake stops (1Day) or correct stops
    with twitchy signals (15Min) — never both. Mixed-resolution replay gives it
    both: indicators from the daily series, entries/exits over 15-minute bars."""
    return _uses_daily_only_signals(params) and _has_price_triggered_exit(params)


# Calendar days of history fetched BEFORE the tested window so the daily
# indicators (MACD/RSI/ATR) are defined from day one of the window — the backtest
# equivalent of the live engine's 120-day MACD lookback. ~150 days ≈ 100 trading
# bars, comfortably above MACD's slow+signal warm-up.
WARMUP_DAYS = 150


def _needs_warmup(params: dict) -> bool:
    """Whether the strategy uses a daily indicator (MACD / RSI / ATR) that needs
    prior history to be defined — if so, the backtest fetches WARMUP_DAYS before
    the window so those signals aren't dead for the window's first ~35 bars."""
    entry = params.get("entry") or {}
    exit_rules = params.get("exit") or {}
    atr = params.get("atr") or {}
    return bool(
        entry.get("require_macd_bullish")
        or exit_rules.get("exit_on_macd_bearish")
        or float(entry.get("rsi_min", 0) or 0) > 0
        or float(entry.get("rsi_max", 0) or 0) > 0
        or float(exit_rules.get("exit_rsi_above", 0) or 0) > 0
        or float(atr.get("stop_mult", 0) or 0) > 0
        or float(atr.get("risk_usd", 0) or 0) > 0
    )


@router.post("/portfolio")
async def run_portfolio(
    body: PortfolioBacktestBody,
    session: Session = Depends(get_session),
    client: AlpacaClient = Depends(require_client),
) -> dict:
    """Portfolio (multi-strategy) backtest: replay N strategies over ONE shared
    account and the global rails, exactly like the live engine. Returns the
    portfolio equity curve + metrics + a per-strategy contribution breakdown."""
    # De-dupe while preserving the order the caller selected them in.
    seen: set[int] = set()
    ids = [sid for sid in body.strategy_ids if not (sid in seen or seen.add(sid))]
    strategies = [session.get(Strategy, sid) for sid in ids]
    missing = [sid for sid, s in zip(ids, strategies) if s is None]
    if missing:
        raise HTTPException(status_code=404, detail=f"Strategy not found: {missing}.")

    if body.timeframe == "1Day" and any(
        json.loads(s.params).get("entry", {}).get("require_above_vwap") for s in strategies
    ):
        raise HTTPException(
            status_code=422,
            detail="A selected strategy uses the VWAP rule, which needs intraday bars — pick 1Hour or 15Min.",
        )
    if body.timeframe in ("15Min", "1Hour") and any(
        _uses_daily_only_signals(json.loads(s.params)) for s in strategies
    ):
        raise HTTPException(
            status_code=422,
            detail="A selected strategy uses MACD/RSI, which are daily signals — on intraday bars "
            "they whipsaw and won't match the live engine. Use 1 Day.",
        )

    # Resolve each strategy's own universe, then fetch bars ONCE per asset class.
    symbols_by_strategy = {s.id: _strategy_symbols(session, s) for s in strategies}
    if not any(symbols_by_strategy.values()):
        raise HTTPException(
            status_code=422,
            detail="None of those strategies resolved to any symbols — add symbols to a watchlist/basket, "
            "or pick custom-universe strategies.",
        )
    total_symbols = len({sym for syms in symbols_by_strategy.values() for sym in syms})
    if total_symbols > 40:
        raise HTTPException(status_code=422, detail="Too many symbols across those strategies (max 40; rate limits).")

    # Warm-up the daily indicators before the window (if any strategy uses them),
    # so MACD/RSI/ATR are live from day one of the tested window.
    window_start = datetime.now(timezone.utc) - timedelta(days=body.days)
    # Only DAILY bars need warm-up: on daily bars body.days calendar days yield
    # ~35 fewer usable bars once MACD warms up, hence the dead zone. Intraday
    # windows already hold plenty of bars for any indicator (and MACD/RSI are
    # 422-blocked there anyway), so warm-up is a daily-only concern.
    warmup = (
        WARMUP_DAYS
        if body.timeframe == "1Day" and any(_needs_warmup(json.loads(s.params)) for s in strategies)
        else 0
    )
    start = (window_start - timedelta(days=warmup)).strftime("%Y-%m-%dT%H:%M:%SZ")
    by_class: dict[str, list[str]] = {}
    for s in strategies:
        by_class.setdefault(s.asset_class, [])
        for sym in symbols_by_strategy[s.id]:
            if sym not in by_class[s.asset_class]:
                by_class[s.asset_class].append(sym)
    bars_cache: dict[str, dict[str, list]] = {}
    try:
        for asset_class, syms in by_class.items():
            if syms:
                # Read-through the bar cache, like the single-strategy run. This
                # is the HEAVIEST fetch in the app — every symbol of every picked
                # strategy — and it was the one path still re-downloading the same
                # history on every run.
                bars_cache[asset_class] = await barfetch.fetch_bars(
                    client, syms, asset_class, body.timeframe, start
                )
    except AlpacaError as exc:
        raise HTTPException(status_code=502, detail=f"Bar download failed ({exc.status_code}): {exc}")

    bars_by_strategy = {
        s.id: {sym: bars_cache.get(s.asset_class, {}).get(sym, []) for sym in symbols_by_strategy[s.id]}
        for s in strategies
    }
    strategy_dicts = [
        {
            "id": s.id,
            "name": s.name,
            "asset_class": s.asset_class,
            "swing_mode": s.swing_mode,
            "sizing_usd": s.sizing_usd,
            "sleeve_usd": s.sleeve_usd,
            "max_positions": s.max_positions,
            "params": json.loads(s.params),
        }
        for s in strategies
    ]
    # Day bucketing: all-crypto portfolios key by the UTC day; any stock present
    # keys by the ET session day (mixed books use the stock convention).
    market = "crypto" if all(s.asset_class == "crypto" for s in strategies) else "stock"

    # OFF the event loop — see the note on run()'s call. get_risk() returns a
    # plain dict, so nothing DB-bound crosses the thread boundary.
    _report("Replaying the portfolio…")
    result = await asyncio.to_thread(
        backtest.run_portfolio_backtest,
        strategy_dicts, bars_by_strategy, get_risk(session),
        starting_cash=body.starting_cash, spread_pct=body.spread_pct, market=market,
        sim_start=window_start if warmup else None,
    )
    if "error" in result:
        raise HTTPException(status_code=422, detail=result["error"])

    result["timeframe"] = body.timeframe
    result["days"] = body.days
    return result


@router.post("")
async def run(
    body: BacktestBody,
    session: Session = Depends(get_session),
    client: AlpacaClient = Depends(require_client),
) -> dict:
    strategy = session.get(Strategy, body.strategy_id)
    if not strategy:
        raise HTTPException(status_code=404, detail="Strategy not found.")

    strategy_dict = {
        "asset_class": strategy.asset_class,
        "swing_mode": strategy.swing_mode,
        "sizing_usd": strategy.sizing_usd,
        "sleeve_usd": strategy.sleeve_usd,
        "max_positions": strategy.max_positions,
        "params": json.loads(strategy.params),
    }

    if body.scanner_replay:
        return await _scanner_replay(body, strategy, strategy_dict, session, client)

    symbols = [s.strip().upper() for s in body.symbols if s.strip()]
    if not symbols:
        symbols = [
            i.symbol
            for i in session.query(WatchlistItem)
            .filter(WatchlistItem.asset_class == strategy.asset_class)
            .all()
        ]
    if not symbols:
        raise HTTPException(
            status_code=422,
            detail="No symbols: pass some, or add symbols to the watchlist for this asset class.",
        )
    if len(symbols) > 50:
        raise HTTPException(status_code=422, detail="Max 50 symbols per backtest (rate limits).")

    params = strategy_dict["params"]
    # Daily signals + a price-triggered exit → replay INTRADAY with the indicators
    # taken from the daily series (see _mixed_resolution). This is a property of
    # the strategy, not of the requested bar size, so it's decided here and the
    # replay timeframe follows from it.
    mixed = _mixed_resolution(params)

    if body.timeframe == "1Day" and params.get("entry", {}).get("require_above_vwap"):
        raise HTTPException(
            status_code=422,
            detail="This strategy uses the VWAP rule, which needs intraday bars — pick 1Hour or 15Min.",
        )
    if body.timeframe in ("15Min", "1Hour") and _uses_daily_only_signals(params) and not mixed:
        # Still wrong for a MACD/RSI strategy with no price-triggered exit: there
        # is nothing an intraday replay would buy us, and the indicators would be
        # computed off intraday closes. Mixed-resolution runs are exempt — they
        # replay intraday precisely BECAUSE the signals stay daily.
        raise HTTPException(
            status_code=422,
            detail="This strategy uses MACD/RSI, which are daily signals — on intraday bars they "
            "whipsaw and won't match the live engine. Use 1 Day.",
        )

    # Fetch WARM-UP history before the window when the strategy uses daily
    # indicators, so MACD/RSI/ATR are defined from day one of the tested window
    # (the sim ignores warm-up bars for trading — see run_backtest's sim_start).
    window_start = datetime.now(timezone.utc) - timedelta(days=body.days)
    needs_warmup = _needs_warmup(params)
    warmup = WARMUP_DAYS if (body.timeframe == "1Day" or mixed) and needs_warmup else 0
    fetch_start = (window_start - timedelta(days=warmup)).strftime("%Y-%m-%dT%H:%M:%SZ")
    window_start_str = window_start.strftime("%Y-%m-%dT%H:%M:%SZ")
    # The bar size actually REPLAYED. Mixed resolution always replays 15-minute
    # bars — the finest the free feed gives — whatever the caller asked for.
    replay_timeframe = "15Min" if mixed else body.timeframe
    daily_bars: dict[str, list[dict]] | None = None
    # Read-through the bar cache (qt.services.barfetch): the same year of history
    # was being re-downloaded on every run. Only the missing recent edge is
    # fetched, and any cache trouble degrades to a plain Alpaca fetch.
    _report(f"Downloading {len(symbols)} symbol{'' if len(symbols) == 1 else 's'} of history…")
    try:
        if mixed:
            # Two fetches, deliberately different windows: the DAILY series reaches
            # back over the warm-up so the indicators are defined from day one,
            # while the intraday series covers only the tested window — 15-minute
            # crypto bars are 96/symbol/day, so fetching warm-up intraday too would
            # multiply the download for bars that could never trade.
            daily_bars = await barfetch.fetch_bars(
                client, symbols, strategy.asset_class, "1Day", fetch_start
            )
            bars = await barfetch.fetch_bars(
                client, symbols, strategy.asset_class, replay_timeframe, window_start_str
            )
        else:
            bars = await barfetch.fetch_bars(
                client, symbols, strategy.asset_class, replay_timeframe, fetch_start
            )
    except AlpacaError as exc:
        raise HTTPException(status_code=502, detail=f"Bar download failed ({exc.status_code}): {exc}")

    # Day bucketing must be the SAME on both sides of a mixed run, or the daily
    # series and the intraday bars disagree about which day a bar belongs to and
    # the look-ahead frontier leaks. Crypto daily bars are stamped 00:00Z, so
    # crypto buckets by the UTC day — as the live engine, the optimizer and the
    # scanner replay all do.
    #
    # This used to apply only to MIXED crypto runs, "keeping the historical
    # 'stock' default untouched" for the rest. That default was wrong: bucketing
    # a 00:00Z daily bar by ET files it under the PREVIOUS day, and it left the
    # plain crypto backtest as the only place in the app not using the live
    # convention — so the optimizer tuned against one definition of a day and the
    # backtest graded against another.
    market = "crypto" if strategy.asset_class == "crypto" else "stock"
    # Run the replay OFF the event loop. It is pure CPU over every bar — a
    # 350-day, 30-symbol run is hundreds of thousands of them — and a coroutine
    # that never awaits owns the loop for its whole duration. That would freeze
    # the engine tick, every other request, AND the very /job polls that exist to
    # keep each request short, so the proxy would time out anyway. A thread lets
    # the loop breathe (Python drops the GIL periodically), which is all the
    # poller needs; get_risk() is a plain dict, so nothing DB-bound crosses over.
    _report("Replaying history…", 0)
    result = await asyncio.to_thread(
        backtest.run_backtest,
        strategy_dict, bars, get_risk(session),
        starting_cash=body.starting_cash, spread_pct=body.spread_pct,
        fee_pct=(
            body.fee_pct if body.fee_pct is not None else DEFAULT_FEE_PCT.get(strategy.asset_class, 0.0)
        ),
        market=market,
        # Mixed runs fetch intraday bars for the window only, so sim_start is a
        # belt-and-braces guard: nothing before the window can ever trade.
        sim_start=window_start if (warmup or mixed) else None,
        daily_bars_by_symbol=daily_bars,
        progress=_replay_progress,
    )
    if "error" in result:
        raise HTTPException(status_code=422, detail=result["error"])

    # The market benchmark is only informative when it's a DIFFERENT asset from
    # the one being traded. Testing BTC/USD against a "market" of BTC/USD drew
    # the same asset twice (and disagreed with itself, being sampled from daily
    # bars rather than the strategy's own). Skip it — and save the API call.
    market_symbol = "SPY" if strategy.asset_class == "stock" else "BTC/USD"
    result["benchmark"] = None
    result["benchmark_symbol"] = None
    if [market_symbol] != symbols:
        _report(f"Fetching the {market_symbol} benchmark…")
        try:
            result["benchmark"] = await backtest.fetch_benchmark(
                # Same day bucketing as the run, or the benchmark line lands a day
                # off the equity curve (only differs for a crypto mixed run).
                client, strategy.asset_class, window_start_str, result["equity_days"],
                market=market,
            )
            result["benchmark_symbol"] = market_symbol
        except Exception:
            result["benchmark"] = None
            result["benchmark_symbol"] = None

    result["strategy_name"] = strategy.name
    result["symbols"] = symbols
    # `timeframe` is what was REPLAYED (what the stops were checked on); on a
    # mixed run the signals came from a coarser series, named separately.
    result["timeframe"] = replay_timeframe
    result["mixed_resolution"] = mixed
    if mixed:
        result["signal_timeframe"] = "1Day"
    result["days"] = body.days
    return result


async def _scanner_replay(
    body: BacktestBody, strategy: Strategy, strategy_dict: dict, session: Session, client: AlpacaClient
) -> dict:
    """Replay the historical 'today's risers' the scanner would have surfaced:
    for each past day, only that day's cached top-N movers are eligible to
    enter. Prefers cached INTRADAY bars (so intraday exits — flatten-before-
    close, VWAP, the entry window — behave for real); falls back to daily bars
    when no intraday sweep has been run. Fully offline. Works for stocks (ET
    session days) and crypto (UTC calendar days) off their SEPARATE caches."""
    # Both off the event loop: the dataset read sweeps the whole bar cache and the
    # replay is pure CPU. See the note on run()'s call.
    _report("Reading cached movers…")
    from qt.services import scanner as scanner_svc

    cfg = scanner_svc.get_config(session)
    ds = await asyncio.to_thread(
        load_scanner_replay_dataset, strategy.asset_class, body.days, body.replay_top_n, cfg
    )

    _report("Replaying history…", 0)
    result = await asyncio.to_thread(
        backtest.run_backtest,
        strategy_dict, ds.bars, get_risk(session),
        starting_cash=body.starting_cash, spread_pct=body.spread_pct,
        fee_pct=(
            body.fee_pct if body.fee_pct is not None else DEFAULT_FEE_PCT.get(strategy.asset_class, 0.0)
        ),
        eligible_by_day=ds.eligible_by_day, market=ds.market,
        progress=_replay_progress,
    )
    if "error" in result:
        raise HTTPException(status_code=422, detail=result["error"])

    # Broad-market benchmark (SPY for stocks, BTC/USD for crypto), best-effort —
    # the tested-symbols hold benchmark is meaningless here (many names), so drop it.
    result["hold_benchmark"] = None
    result["hold_benchmark_label"] = None
    result["benchmark"] = None
    result["benchmark_symbol"] = None
    try:
        result["benchmark"] = await backtest.fetch_benchmark(
            client, ds.benchmark_class, ds.start_day, result["equity_days"], market=ds.market
        )
        result["benchmark_symbol"] = ds.benchmark_symbol
    except Exception:
        pass

    result["strategy_name"] = strategy.name
    result["scanner_replay"] = True
    result["replay_intraday"] = ds.used_intraday
    result["replay_top_n"] = body.replay_top_n
    result["universe_size"] = len(ds.replayed)  # what was TESTED
    result["universe_dropped"] = ds.dropped
    result["intraday_covered"] = ds.intraday_covered
    result["days_replayed"] = ds.days_replayed
    timeframe = ds.timeframe
    result["symbols"] = []  # too many to list; summarized by universe_size
    result["timeframe"] = timeframe
    result["days"] = body.days
    return result


# ---------------------------------------------------------------------------
# Background jobs
#
# A replay that takes minutes cannot live inside an HTTP request: nginx's default
# read timeout is 60s and Cloudflare's is a FIXED 100s (it answers HTTP 524, and
# no plan setting raises it). Start the same handler as a task, hand back an id,
# and let the browser poll — every request then finishes in milliseconds.
#
# In-process and deliberately not persisted: a backtest is a read-only
# experiment, so losing jobs on restart just means running it again.
# ---------------------------------------------------------------------------

JOB_TTL_SECONDS = 900  # keep a finished job around this long for the poller
MAX_JOBS = 12


@dataclass
class BacktestJob:
    id: str
    kind: str  # "single" | "portfolio"
    running: bool = True
    # What it is doing right now, so a multi-minute run shows a pulse rather
    # than a frozen button. pct covers the replay only — the phase that has a
    # knowable length.
    phase: str = "Starting…"
    pct: int | None = None
    started_at: str = ""
    finished_at: str | None = None
    error: str | None = None
    # Carried so the browser can tell "your strategy is misconfigured" (422) from
    # "Alpaca is down" (502) — the same distinction the direct endpoint gives.
    status_code: int | None = None
    result: dict | None = field(default=None)


_jobs: dict[str, BacktestJob] = {}
_tasks: set[asyncio.Task] = set()


def _prune_jobs() -> None:
    """Drop finished jobs the browser is done with. Results carry full equity
    curves and trade lists, so they are not something to accumulate forever."""
    now = datetime.now(timezone.utc)
    for job_id, job in list(_jobs.items()):
        if job.running or not job.finished_at:
            continue
        age = (now - datetime.fromisoformat(job.finished_at)).total_seconds()
        if age > JOB_TTL_SECONDS:
            del _jobs[job_id]
    # Backstop: a browser that never polls would otherwise pile results up.
    if len(_jobs) > MAX_JOBS:
        finished = sorted(
            (j for j in _jobs.values() if not j.running),
            key=lambda j: j.finished_at or "",
        )
        for job in finished[: len(_jobs) - MAX_JOBS]:
            _jobs.pop(job.id, None)


def _new_job(kind: str) -> BacktestJob:
    _prune_jobs()
    job = BacktestJob(
        id=uuid.uuid4().hex, kind=kind, started_at=datetime.now(timezone.utc).isoformat()
    )
    _jobs[job.id] = job
    return job


async def _run_job(job: BacktestJob, handler, body, client: AlpacaClient) -> None:
    """Run one of the request handlers with a session of the JOB's own.

    The request's session is closed the moment the start endpoint returns, so the
    task opens its own. Reusing the handlers verbatim is the point: there is one
    implementation of a backtest, and polling must not become a second one that
    can drift from it.
    """
    def sink(phase: str, pct: int | None) -> None:
        job.phase = phase
        job.pct = pct

    _reporter.set(sink)
    try:
        with session_scope() as session:
            job.result = await handler(body, session, client)
    except HTTPException as exc:
        job.error = str(exc.detail)
        job.status_code = exc.status_code
    except AlpacaError as exc:
        log.exception("backtest job: bar download failed")
        job.error = f"Bar download failed ({exc.status_code}): {exc}"
        job.status_code = 502
    except asyncio.CancelledError:
        # Never leave a cancelled job looking like a finished one with no result —
        # that is the silent failure this whole change exists to remove.
        job.error = "The backtest was cancelled (the server shut down or reloaded). Run it again."
        job.status_code = 503
        raise
    except Exception as exc:  # noqa: BLE001 — surface any failure to the poller
        log.exception("backtest job failed")
        job.error = str(exc) or exc.__class__.__name__
        job.status_code = 500
    finally:
        job.running = False
        job.finished_at = datetime.now(timezone.utc).isoformat()


def _spawn(job: BacktestJob, handler, body, client: AlpacaClient) -> None:
    task = asyncio.create_task(_run_job(job, handler, body, client))
    # Without a strong reference the loop may garbage-collect a running task.
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


@router.post("/start")
async def start_backtest(
    body: BacktestBody,
    session: Session = Depends(get_session),
    client: AlpacaClient = Depends(require_client),
) -> dict:
    """Start a single-strategy backtest in the background; poll /job/{id}."""
    # Cheap existence check up front so a bad id still fails immediately, the way
    # it did before. Everything else is validated by the handler itself.
    if not session.get(Strategy, body.strategy_id):
        raise HTTPException(status_code=404, detail="Strategy not found.")
    job = _new_job("single")
    _spawn(job, run, body, client)
    return {"job_id": job.id}


@router.post("/portfolio/start")
async def start_portfolio_backtest(
    body: PortfolioBacktestBody,
    session: Session = Depends(get_session),
    client: AlpacaClient = Depends(require_client),
) -> dict:
    """Start a portfolio backtest in the background; poll /job/{id}."""
    if not body.strategy_ids:
        raise HTTPException(status_code=422, detail="Pick at least one strategy.")
    job = _new_job("portfolio")
    _spawn(job, run_portfolio, body, client)
    return {"job_id": job.id}


@router.get("/job/{job_id}")
def backtest_job(job_id: str) -> dict:
    """Poll a background backtest. While it runs the result is null; when it
    finishes, exactly one of `result` / `error` is set."""
    job = _jobs.get(job_id)
    if job is None:
        # Expired, or lost to a restart. Say which — "not found" alone reads like
        # a bug, and the honest answer is that the run has to be repeated.
        raise HTTPException(
            status_code=404,
            detail="That backtest is no longer available (it expired, or the server restarted). Run it again.",
        )
    return {
        "job_id": job.id,
        "kind": job.kind,
        "running": job.running,
        "phase": job.phase,
        "pct": job.pct,
        "started_at": job.started_at,
        "finished_at": job.finished_at,
        "error": job.error,
        "status_code": job.status_code,
        "result": job.result,
    }
