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

import asyncio
import logging
from collections.abc import Callable
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import func
from sqlalchemy.orm import Session

from qt.services import barcache
from qt.services.barcache import (
    CryptoDailyBar,
    CryptoDailyMover,
    CryptoIntradayBar,
    DailyBar,
    DailyMover,
    DayQuote,
    IntradayBar,
)

log = logging.getLogger("qt.barsweep")


async def _bars_with_retry(
    client, symbols, timeframe, start_iso, end_iso=None, *,
    asset_class: str = "stock", attempts: int = 3, retry_delay: float = 2.0,
):
    """Fetch bars, retrying on ANY error (a 429 rate-limit, a request timeout, a
    dropped connection) with exponential backoff. A long sweep makes thousands of
    calls, so transient failures are expected — retrying keeps one hiccup from
    losing a day's data. Raises the last error only if every attempt fails.
    `asset_class` ('stock' | 'crypto') routes to the right Alpaca bars feed."""
    delay = retry_delay
    for attempt in range(1, attempts + 1):
        try:
            return await client.historical_bars(symbols, asset_class, timeframe, start_iso, end_iso)
        except Exception as exc:  # noqa: BLE001 — rate limits/timeouts/network are all retryable
            if attempt == attempts:
                raise
            log.warning("bars fetch failed (attempt %s/%s): %s — retrying in %.0fs", attempt, attempts, exc, delay)
            await asyncio.sleep(delay)
            delay *= 2

# Store a GENEROUS set of risers per day, not the live scanner's display count.
# The backtest narrows to its chosen top-N at read time (barcache.movers_between),
# so widening/narrowing the replay's riser count never needs a re-sweep — the
# expensive part (downloading bars) is decoupled from the cheap ranking knob.
SWEEP_STORE_TOP_N = 100

# Real exchanges we keep. Alpaca us_equity `exchange` values are things like
# NYSE, NASDAQ, ARCA, AMEX, BATS, OTC (and occasionally blank). Momentum movers
# are often obscure small-caps, so we deliberately DON'T pre-restrict to
# large-caps — but OTC/pink-sheet junk (and blank exchanges) are excluded,
# exactly as the live scanner's $1 "penny/OTC junk" price floor intends.
KEEP_EXCHANGES = {"NYSE", "NASDAQ", "ARCA", "AMEX", "BATS", "NYSEARCA", "IEX"}

# progress(batches_done, batches_total, symbols_saved) — optional live hook.
ProgressFn = Callable[[int, int, int], None]
# Intraday adds a running bar count: progress(day_done, day_total, symbols_saved, bars_saved).
IntradayProgressFn = Callable[[int, int, int, int], None]
# Reconstruct reports two stages: progress("load"|"rank", done, total).
ReconstructProgressFn = Callable[[str, int, int], None]


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
    retry_delay: float = 2.0,
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
            data = await _bars_with_retry(client, chunk, "1Day", start_iso, retry_delay=retry_delay)
        except Exception as exc:  # noqa: BLE001 — after retries, skip this batch, never abort the sweep
            errors += 1
            status = getattr(exc, "status_code", "—")
            log.warning("sweep batch %s/%s failed (%s): %s", idx, len(batches), status, exc)
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
    progress: ReconstructProgressFn | None = None,
    daily_model=DailyBar,
    mover_model=DailyMover,
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
    None (the default) rebuilds the whole cache — the initial sweep's behaviour.

    `progress("load"|"rank", done, total)` reports the two phases (loading the
    cached bars, then ranking the days) so the UI can show real progress instead
    of an opaque wait. Bars are streamed (`yield_per`) rather than loaded all at
    once, which also eases memory on a big cache."""
    q = sess.query(daily_model)
    if since_day is not None:
        load_from = (date.fromisoformat(since_day) - timedelta(days=lookback_days)).isoformat()
        q = q.filter(daily_model.day >= load_from)
    total_rows = q.count() if progress else 0

    quotes_by_day: dict[str, list[DayQuote]] = {}
    prev_symbol: str | None = None
    prev_close: float | None = None
    for i, bar in enumerate(q.order_by(daily_model.symbol, daily_model.day).yield_per(5000), start=1):
        if bar.symbol != prev_symbol:
            prev_symbol = bar.symbol
            prev_close = None
        if prev_close is not None:
            quotes_by_day.setdefault(bar.day, []).append(
                DayQuote(
                    symbol=bar.symbol, close=bar.c, prev_close=prev_close,
                    volume=bar.v, vwap=bar.vw, high=bar.h,  # rank on the intraday peak
                )
            )
        prev_close = bar.c
        if progress and (i % 5000 == 0 or i == total_rows):
            progress("load", i, total_rows)

    days = [d for d in sorted(quotes_by_day) if since_day is None or d >= since_day]
    for j, day in enumerate(days, start=1):
        ranked = barcache.rank_movers(
            quotes_by_day[day],
            top_n,
            min_change_pct=min_change_pct,
            min_price=min_price,
            max_price=max_price,
            min_dollar_volume=min_dollar_volume,
        )
        barcache.store_movers(sess, day, ranked, model=mover_model)
        if progress and (j % 20 == 0 or j == len(days)):
            progress("rank", j, len(days))
    sess.commit()
    return len(days)


async def sweep_intraday_movers(
    client,
    sess: Session,
    *,
    timeframe: str = "15Min",
    baseline_days: int = 4,
    since_day: str | None = None,
    progress: IntradayProgressFn | None = None,
    retry_delay: float = 2.0,
    asset_class: str = "stock",
    mover_model=DailyMover,
    intraday_model=IntradayBar,
) -> dict:
    """Stage 2: pull intraday bars for the reconstructed movers so an intraday
    strategy can be replayed on how each day actually unfolded.

    Batches by DAY — one paginated request per mover-day fetches that day's
    ~top-N symbols at once. Each request starts `baseline_days` before the
    mover-day so the prior trading session's close is present: the backtester
    derives the day-gain from the previous day's close within the series, so
    without that baseline the first mover-day couldn't qualify. `since_day`
    limits the sweep to recent days (the forward job). Resilient: a day that
    errors is logged and skipped.

    `asset_class`/`mover_model`/`intraday_model` select the stock (default) or
    crypto side — the batching/resume logic is identical for both."""
    q = sess.query(mover_model.day, mover_model.symbol)
    if since_day is not None:
        q = q.filter(mover_model.day >= since_day)
    by_day: dict[str, list[str]] = {}
    for day, symbol in q.order_by(mover_model.day).all():
        by_day.setdefault(day, []).append(symbol)

    days = sorted(by_day)
    # Resumable: skip mover-days already pulled. Bars are committed per day, so a
    # day is either fully cached or not started — a re-run after an interruption
    # (or a rate-limit-heavy stretch) continues where it stopped instead of
    # re-fetching everything and re-hitting the wall.
    done = {row[0] for row in sess.query(func.substr(intraday_model.ts, 1, 10)).distinct().all()}
    bars_saved = 0
    symbols_saved = 0
    errors = 0
    skipped = 0
    for idx, day in enumerate(days, start=1):
        if day in done:
            skipped += 1
            if progress:
                progress(idx, len(days), symbols_saved, bars_saved)
            continue
        symbols = sorted(set(by_day[day]))
        start = (date.fromisoformat(day) - timedelta(days=baseline_days)).isoformat()
        end = (date.fromisoformat(day) + timedelta(days=1)).isoformat()
        try:
            data = await _bars_with_retry(
                client, symbols, timeframe, start, end, asset_class=asset_class, retry_delay=retry_delay
            )
        except Exception as exc:  # noqa: BLE001 — persistent failure after retries: skip the day, don't abort
            errors += 1
            status = getattr(exc, "status_code", "—")
            log.warning("intraday sweep %s (%s/%s) skipped after retries (%s): %s", day, idx, len(days), status, exc)
            if progress:
                progress(idx, len(days), symbols_saved, bars_saved)
            continue
        for symbol, bars in data.items():
            if not bars:
                continue
            bars_saved += barcache.save_intraday_bars(sess, symbol, bars, model=intraday_model)
            symbols_saved += 1
        sess.commit()
        if progress:
            progress(idx, len(days), symbols_saved, bars_saved)

    return {
        "days": len(days),
        "symbols_saved": symbols_saved,
        "bars_saved": bars_saved,
        "errors": errors,
        "skipped": skipped,
        "timeframe": timeframe,
    }


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
    out = {**swept, "days_reconstructed": days, "since_day": since}
    # Keep intraday current too, but only if the user already built an intraday
    # cache — same "maintain, never bootstrap" rule as the daily forward job.
    if barcache.has_intraday(sess):
        out["intraday"] = await sweep_intraday_movers(client, sess, since_day=since)
    return out


# ---------------------------------------------------------------------------
# CRYPTO sweep. The crypto universe is TINY (~20-40 USD pairs), so there's no
# need for the stock two-stage "find movers, then pull intraday only for them"
# optimization: we just sweep ALL pairs' daily AND 15-min bars directly (cheap).
# We still reconstruct daily movers so the replay's per-day eligibility gate
# works exactly as it does for stocks. Crypto "days" are UTC calendar days.
# ---------------------------------------------------------------------------


def crypto_universe(assets: list[dict]) -> list[str]:
    """The crypto sweep universe: tradable USD pairs (symbols like 'BTC/USD').
    Non-USD quote pairs (BTC/USDT, ETH/BTC, …) are excluded — the scanner and
    live engine trade USD pairs, so the replay universe must match."""
    symbols: set[str] = set()
    for a in assets:
        if not a.get("tradable"):
            continue
        symbol = a.get("symbol")
        if symbol and symbol.endswith("/USD"):
            symbols.add(symbol)
    return sorted(symbols)


async def sweep_crypto_daily_bars(
    client,
    sess: Session,
    days: int = 365,
    batch_size: int = 20,
    *,
    progress: ProgressFn | None = None,
    retry_delay: float = 2.0,
) -> dict:
    """Download ~`days` of daily bars for the whole crypto USD-pair universe and
    store them in the crypto bar cache. Mirrors sweep_daily_bars, but the tiny
    universe means one or two batches. Crypto daily bars are UTC-aligned, so
    save_daily_bars keys them by the UTC calendar day automatically. Resilient:
    a batch that errors is logged and skipped, never aborting the sweep."""
    assets = await client.list_assets("crypto")
    symbols = crypto_universe(assets)
    start_iso = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    # Crypto trades 24/7, so "today's" UTC daily bar is always still forming.
    # save_daily_bars is insert-or-ignore: cache that partial (near-open) bar and
    # it's frozen there forever, corrupting the day's change_pct. Only completed
    # UTC days are cacheable — drop today. (Stocks avoid this by sweeping after
    # the 16:00 ET close, when the day's bar is final.)
    today_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    batches = [symbols[i : i + batch_size] for i in range(0, len(symbols), batch_size)]
    symbols_saved = 0
    errors = 0
    for idx, chunk in enumerate(batches, start=1):
        try:
            data = await _bars_with_retry(
                client, chunk, "1Day", start_iso, asset_class="crypto", retry_delay=retry_delay
            )
        except Exception as exc:  # noqa: BLE001 — after retries, skip this batch, never abort the sweep
            errors += 1
            status = getattr(exc, "status_code", "—")
            log.warning("crypto sweep batch %s/%s failed (%s): %s", idx, len(batches), status, exc)
            if progress:
                progress(idx, len(batches), symbols_saved)
            continue
        for symbol, bars in data.items():
            bars = [b for b in bars if (b.get("t") or "")[:10] != today_utc]
            if not bars:
                continue
            barcache.save_daily_bars(sess, symbol, bars, model=CryptoDailyBar)
            symbols_saved += 1
        sess.commit()
        log.info("crypto sweep batch %s/%s done (%s pairs saved so far)", idx, len(batches), symbols_saved)
        if progress:
            progress(idx, len(batches), symbols_saved)

    return {
        "symbols_total": len(symbols),
        "symbols_saved": symbols_saved,
        "batches": len(batches),
        "errors": errors,
    }


def reconstruct_crypto_movers(
    sess: Session,
    *,
    top_n: int = SWEEP_STORE_TOP_N,
    min_change_pct: float,
    min_price: float,
    max_price: float,
    min_dollar_volume: float,
    since_day: str | None = None,
    lookback_days: int = 15,
    progress: ReconstructProgressFn | None = None,
) -> int:
    """reconstruct_movers over the crypto tables (UTC-day movers, peak ranking)."""
    return reconstruct_movers(
        sess,
        top_n=top_n,
        min_change_pct=min_change_pct,
        min_price=min_price,
        max_price=max_price,
        min_dollar_volume=min_dollar_volume,
        since_day=since_day,
        lookback_days=lookback_days,
        progress=progress,
        daily_model=CryptoDailyBar,
        mover_model=CryptoDailyMover,
    )


async def sweep_crypto_intraday(
    client,
    sess: Session,
    *,
    timeframe: str = "15Min",
    baseline_days: int = 4,
    since_day: str | None = None,
    progress: IntradayProgressFn | None = None,
    retry_delay: float = 2.0,
) -> dict:
    """sweep_intraday_movers over the crypto tables (crypto feed, UTC days)."""
    return await sweep_intraday_movers(
        client,
        sess,
        timeframe=timeframe,
        baseline_days=baseline_days,
        since_day=since_day,
        progress=progress,
        retry_delay=retry_delay,
        asset_class="crypto",
        mover_model=CryptoDailyMover,
        intraday_model=CryptoIntradayBar,
    )


async def crypto_daily_movers_update(
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
    """Keep the crypto movers cache current going forward — the crypto twin of
    daily_movers_update. Sweeps the last `overlap_days` of every USD pair's daily
    bars, re-ranks those recent UTC days, and (only if a crypto intraday cache
    already exists) refreshes intraday too. Same 'maintain, never bootstrap' rule."""
    swept = await sweep_crypto_daily_bars(client, sess, days=overlap_days)
    since = (datetime.now(timezone.utc) - timedelta(days=overlap_days)).strftime("%Y-%m-%d")
    days = reconstruct_crypto_movers(
        sess,
        top_n=top_n,
        min_change_pct=min_change_pct,
        min_price=min_price,
        max_price=max_price,
        min_dollar_volume=min_dollar_volume,
        since_day=since,
    )
    out = {**swept, "days_reconstructed": days, "since_day": since}
    if barcache.has_intraday(sess, model=CryptoIntradayBar):
        out["intraday"] = await sweep_crypto_intraday(client, sess, since_day=since)
    return out
