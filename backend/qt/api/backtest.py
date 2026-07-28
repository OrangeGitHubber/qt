"""Backtest endpoint: replay a saved strategy over history."""

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from qt.api.market import require_client
from qt.broker.alpaca import AlpacaClient, AlpacaError
from qt.db import get_session
from qt.models import BasketItem, Strategy, WatchlistItem
from qt.services import backtest
from qt.services.engine import get_risk

router = APIRouter(prefix="/api/backtest", tags=["backtest"])

TIMEFRAMES = ("15Min", "1Hour", "1Day")


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


def load_scanner_replay_dataset(asset_class: str, days: int, replay_top_n: int) -> ScannerReplayDataset:
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
    benchmark_symbol = "BTC/USD" if crypto else "SPY"

    try:
        barcache.init_cache()
    except Exception as exc:  # noqa: BLE001 — surface a bad cache DSN clearly
        raise HTTPException(status_code=502, detail=f"Could not open the bar cache DB: {exc}")

    start_day = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")
    cache = barcache.session()
    try:
        movers = barcache.movers_between(cache, start_day, top_n=replay_top_n, model=mover_model)
        if not movers:
            asset = "crypto" if crypto else "stock"
            raise HTTPException(
                status_code=422,
                detail=f"No cached {asset} movers yet — run a {'crypto ' if crypto else ''}sweep first "
                       "(Settings → Historical bar cache).",
            )
        eligible_by_day = {day: set(syms) for day, syms in movers.items()}
        union = sorted({s for syms in movers.values() for s in syms})
        intraday_bars = barcache.cached_intraday_bars(cache, union, start_day, model=intraday_model)
        used_intraday = any(intraday_bars.values())
        if used_intraday:
            bars = intraday_bars
            timeframe = "15Min"
        else:
            bars = barcache.cached_daily_bars(cache, union, start_day, model=daily_model, stamp=daily_stamp)
            timeframe = "1Day"
    finally:
        cache.close()

    return ScannerReplayDataset(
        bars=bars, eligible_by_day=eligible_by_day, timeframe=timeframe,
        used_intraday=used_intraday, union=union, market=market,
        benchmark_class=market, benchmark_symbol=benchmark_symbol,
        start_day=start_day, days_replayed=len(movers),
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

    start = (datetime.now(timezone.utc) - timedelta(days=body.days)).strftime("%Y-%m-%dT%H:%M:%SZ")
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
                bars_cache[asset_class] = await client.historical_bars(syms, asset_class, body.timeframe, start)
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

    result = backtest.run_portfolio_backtest(
        strategy_dicts, bars_by_strategy, get_risk(session),
        starting_cash=body.starting_cash, spread_pct=body.spread_pct, market=market,
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
    if len(symbols) > 25:
        raise HTTPException(status_code=422, detail="Max 25 symbols per backtest (rate limits).")

    if body.timeframe == "1Day" and json.loads(strategy.params).get("entry", {}).get("require_above_vwap"):
        raise HTTPException(
            status_code=422,
            detail="This strategy uses the VWAP rule, which needs intraday bars — pick 1Hour or 15Min.",
        )

    start = (datetime.now(timezone.utc) - timedelta(days=body.days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        bars = await client.historical_bars(symbols, strategy.asset_class, body.timeframe, start)
    except AlpacaError as exc:
        raise HTTPException(status_code=502, detail=f"Bar download failed ({exc.status_code}): {exc}")

    result = backtest.run_backtest(
        strategy_dict, bars, get_risk(session),
        starting_cash=body.starting_cash, spread_pct=body.spread_pct,
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
        try:
            result["benchmark"] = await backtest.fetch_benchmark(
                client, strategy.asset_class, start, result["equity_days"]
            )
            result["benchmark_symbol"] = market_symbol
        except Exception:
            result["benchmark"] = None
            result["benchmark_symbol"] = None

    result["strategy_name"] = strategy.name
    result["symbols"] = symbols
    result["timeframe"] = body.timeframe
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
    ds = load_scanner_replay_dataset(strategy.asset_class, body.days, body.replay_top_n)

    result = backtest.run_backtest(
        strategy_dict, ds.bars, get_risk(session),
        starting_cash=body.starting_cash, spread_pct=body.spread_pct,
        eligible_by_day=ds.eligible_by_day, market=ds.market,
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
    result["universe_size"] = len(ds.union)
    result["days_replayed"] = ds.days_replayed
    timeframe = ds.timeframe
    result["symbols"] = []  # too many to list; summarized by universe_size
    result["timeframe"] = timeframe
    result["days"] = body.days
    return result
