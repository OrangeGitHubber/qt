"""Backtest endpoint: replay a saved strategy over history."""

import json
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from qt.api.market import require_client
from qt.broker.alpaca import AlpacaClient, AlpacaError
from qt.db import get_session
from qt.models import Strategy, WatchlistItem
from qt.services import backtest
from qt.services.engine import get_risk

router = APIRouter(prefix="/api/backtest", tags=["backtest"])

TIMEFRAMES = ("15Min", "1Hour", "1Day")


class BacktestBody(BaseModel):
    strategy_id: int
    symbols: list[str] = []  # empty = use the watchlist for the strategy's asset class
    scanner_replay: bool = False  # replay the cached historical daily top-N risers instead
    days: int = Field(default=90, ge=7, le=730)
    timeframe: str = Field(default="1Hour", pattern="^(15Min|1Hour|1Day)$")
    starting_cash: float = Field(default=5000, ge=100, le=10_000_000)
    spread_pct: float = Field(default=0.1, ge=0, le=2)


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
    enter. Runs on cached DAILY bars — fully offline, no Alpaca fetch (run a
    sweep first in Settings). Stocks only for now."""
    if strategy.asset_class != "stock":
        raise HTTPException(status_code=422, detail="Scanner replay is stocks-only for now.")

    from qt.services import barcache

    start_day = (datetime.now(timezone.utc) - timedelta(days=body.days)).strftime("%Y-%m-%d")
    cache = barcache.session()
    try:
        movers = barcache.movers_between(cache, start_day)
        if not movers:
            raise HTTPException(
                status_code=422,
                detail="No cached movers yet — run a sweep first (Settings → Historical bar cache).",
            )
        eligible_by_day = {day: set(syms) for day, syms in movers.items()}
        union = sorted({s for syms in movers.values() for s in syms})
        bars = barcache.cached_daily_bars(cache, union, start_day)
    finally:
        cache.close()

    result = backtest.run_backtest(
        strategy_dict, bars, get_risk(session),
        starting_cash=body.starting_cash, spread_pct=body.spread_pct,
        eligible_by_day=eligible_by_day,
    )
    if "error" in result:
        raise HTTPException(status_code=422, detail=result["error"])

    # Broad-market benchmark (SPY), best-effort — the tested-symbols hold
    # benchmark is meaningless here (hundreds of names), so drop it.
    result["hold_benchmark"] = None
    result["hold_benchmark_label"] = None
    result["benchmark"] = None
    result["benchmark_symbol"] = None
    try:
        result["benchmark"] = await backtest.fetch_benchmark(client, "stock", start_day, result["equity_days"])
        result["benchmark_symbol"] = "SPY"
    except Exception:
        pass

    result["strategy_name"] = strategy.name
    result["scanner_replay"] = True
    result["universe_size"] = len(union)
    result["days_replayed"] = len(movers)
    result["symbols"] = []  # too many to list; summarized by universe_size
    result["timeframe"] = "1Day"
    result["days"] = body.days
    return result
