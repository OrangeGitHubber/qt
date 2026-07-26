"""Historical universe daily-bar SWEEP + movers reconstruction.

Fills the bar cache (see barcache.py) with a broad-but-not-junk universe of
US-equity daily bars, then reconstructs each past day's "today's risers" the
same way the live scanner would have surfaced them. This is the data feed for
the future "scanner replay" backtest: Alpaca has no historical movers
endpoint, so the risers must be recomputed from stored price history.

The sweep is heavy (thousands of symbols, ~a year of bars each) and is meant
to run on the user's own instance against real Alpaca + their configured cache
DB (SQLite or Postgres). It can't run in dev/CI (dummy keys, no Postgres), so
everything here is broker-agnostic and exercised with a mocked client on
in-memory SQLite.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import date, datetime, timedelta, timezone

from sqlalchemy.orm import Session

from qt.broker.alpaca import AlpacaError
from qt.services import barcache
from qt.services.barcache import DailyBar, DayQuote

log = logging.getLogger("qt.barsweep")

# Store a GENEROUS set of risers per day, not the live scanner's display count.
# The backtest narrows to its chosen top-N at read time (barcache.movers_between),
# so widening/narrowing the replay's riser count never needs a re-sweep — the
# expensive part (downloading bars) is decoupled from the cheap ranking knob.
SWEEP_STORE_TOP_N = 50

# Real exchanges we keep. Alpaca us_equity `exchange` values are things like
# NYSE, NASDAQ, ARCA, AMEX, BATS, OTC (and occasionally blank). Momentum movers
# are often obscure small-caps, so we deliberately DON'T pre-restrict to
# large-caps — but OTC/pink-sheet junk (and blank exchanges) are excluded,
# exactly as the live scanner's $1 "penny/OTC junk" price floor intends.
KEEP_EXCHANGES = {"NYSE", "NASDAQ", "ARCA", "AMEX", "BATS", "NYSEARCA", "IEX"}

# progress(batches_done, batches_total, symbols_saved) — optional live hook.
ProgressFn = Callable[[int, int, int], None]


def tradable_universe(assets: list[dict]) -> list[str]:
    """The broad-but-not-junk stock universe: tradable common stocks on a real
    exchange, OTC/pink-sheet and blank-exchange names excluded."""
    symbols: set[str] = set()
    for a in assets:
        if not a.get("tradable"):
            continue
        if (a.get("exchange") or "").upper() not in KEEP_EXCHANGES:
            continue
        symbol = a.get("symbol")
        if symbol:
            symbols.add(symbol)
    return sorted(symbols)


async def sweep_daily_bars(
    client,
    sess: Session,
    days: int = 365,
    batch_size: int = 100,
    *,
    progress: ProgressFn | None = None,
) -> dict:
    """Download ~`days` of daily bars for the whole stock universe and store
    them in the bar cache. Batches the symbols, commits per batch, and is
    resilient: a batch that errors is logged and skipped, never aborting the
    whole sweep. Returns a summary dict."""
    assets = await client.list_assets("us_equity")
    symbols = tradable_universe(assets)
    start_iso = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()

    batches = [symbols[i : i + batch_size] for i in range(0, len(symbols), batch_size)]
    symbols_saved = 0
    errors = 0
    for idx, chunk in enumerate(batches, start=1):
        try:
            data = await client.historical_bars(chunk, "stock", "1Day", start_iso)
        except AlpacaError as exc:
            errors += 1
            log.warning("sweep batch %s/%s failed (%s): %s", idx, len(batches), exc.status_code, exc)
            if progress:
                progress(idx, len(batches), symbols_saved)
            continue
        for symbol, bars in data.items():
            if not bars:
                continue
            barcache.save_daily_bars(sess, symbol, bars)
            symbols_saved += 1
        sess.commit()
        log.info("sweep batch %s/%s done (%s symbols saved so far)", idx, len(batches), symbols_saved)
        if progress:
            progress(idx, len(batches), symbols_saved)

    return {
        "symbols_total": len(symbols),
        "symbols_saved": symbols_saved,
        "batches": len(batches),
        "errors": errors,
    }


def reconstruct_movers(
    sess: Session,
    *,
    top_n: int = SWEEP_STORE_TOP_N,
    min_change_pct: float,
    min_price: float,
    max_price: float,
    min_dollar_volume: float,
    since_day: str | None = None,
    lookback_days: int = 15,
) -> int:
    """Recompute each past day's 'today's risers' from the cached daily bars.

    For every symbol, its % move on a day is measured against its PREVIOUS
    available bar's close (the prior stored row — so gaps/weekends use the last
    earlier bar, NOT calendar day-1). The earliest day has no prior close and is
    skipped. Filters mirror what the live scanner would have surfaced. Returns
    the number of days reconstructed.

    `since_day` limits the work to recent days (the forward daily job): only
    days on/after it are re-ranked and stored, but bars from `lookback_days`
    before it are still loaded so each of those days has a real prior close.
    None (the default) rebuilds the whole cache — the initial sweep's behaviour."""
    q = sess.query(DailyBar)
    if since_day is not None:
        load_from = (date.fromisoformat(since_day) - timedelta(days=lookback_days)).isoformat()
        q = q.filter(DailyBar.day >= load_from)
    rows = q.order_by(DailyBar.symbol, DailyBar.day).all()

    quotes_by_day: dict[str, list[DayQuote]] = {}
    prev_symbol: str | None = None
    prev_close: float | None = None
    for bar in rows:
        if bar.symbol != prev_symbol:
            prev_symbol = bar.symbol
            prev_close = None
        if prev_close is not None:
            quotes_by_day.setdefault(bar.day, []).append(
                DayQuote(symbol=bar.symbol, close=bar.c, prev_close=prev_close, volume=bar.v, vwap=bar.vw)
            )
        prev_close = bar.c

    days = [d for d in sorted(quotes_by_day) if since_day is None or d >= since_day]
    for day in days:
        ranked = barcache.rank_movers(
            quotes_by_day[day],
            top_n,
            min_change_pct=min_change_pct,
            min_price=min_price,
            max_price=max_price,
            min_dollar_volume=min_dollar_volume,
        )
        barcache.store_movers(sess, day, ranked)
    sess.commit()
    return len(days)


async def daily_movers_update(
    client,
    sess: Session,
    *,
    min_change_pct: float,
    min_price: float,
    max_price: float,
    min_dollar_volume: float,
    top_n: int = SWEEP_STORE_TOP_N,
    overlap_days: int = 5,
) -> dict:
    """Keep the movers cache current going forward. Pulls the last `overlap_days`
    of universe daily bars (a small overlap so a weekend/holiday/missed run is
    caught, deduped by the idempotent upsert) and re-ranks only those recent
    days. Cheap compared to the historical sweep — one handful of days, not a
    year. Returns a summary."""
    swept = await sweep_daily_bars(client, sess, days=overlap_days)
    since = (datetime.now(timezone.utc) - timedelta(days=overlap_days)).strftime("%Y-%m-%d")
    days = reconstruct_movers(
        sess,
        top_n=top_n,
        min_change_pct=min_change_pct,
        min_price=min_price,
        max_price=max_price,
        min_dollar_volume=min_dollar_volume,
        since_day=since,
    )
    return {**swept, "days_reconstructed": days, "since_day": since}
