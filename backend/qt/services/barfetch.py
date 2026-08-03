"""Read-through bar cache for the single-strategy backtest and the optimizer.

Every backtest and every parameter search used to re-download its bars from
Alpaca — the same year of history, over and over, for the same symbols. This
module puts the existing bar cache (qt.services.barcache — SQLite by default,
Postgres via QT_BAR_CACHE_URL) in front of that fetch: read what's already
stored, download only the missing edge, save what came back.

Three rules govern everything here, in this order:

1. NEVER CACHE AN IN-PROGRESS BAR. A bar whose period hasn't closed yet still
   has a moving close. Persist one and every later run reads a wrong price for
   that day forever (the cache is insert-or-ignore, so the bad row would never
   be corrected). Only bars whose period has DEFINITIVELY closed are saved —
   see _closed_daily / _closed_intraday.
2. THE CACHE IS AN OPTIMIZATION, NEVER A DEPENDENCY. Unconfigured, unreachable,
   corrupt, whatever — any failure logs and degrades to a plain Alpaca fetch. A
   backtest must never fail because the cache is down.
3. CORRECTNESS BEFORE THRIFT. A partial series must never be served as if it
   were complete. When a symbol's cached coverage is at all ambiguous (a hole in
   the middle, a missing head) that symbol's whole window is re-fetched.

Scope: the single-strategy backtest and the optimizer. Scanner replay reads the
cache directly (it is an offline replay, not a fetch) and the basket sweep is
left alone.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from qt.services import barcache

log = logging.getLogger("qt.services.barfetch")

# Only these two are cached. 1Hour is a legacy backtest option that nothing
# fetches by default; an uncached timeframe simply passes straight through.
#
# 1Min IS DELIBERATELY ABSENT, and adding it here would be a data-corrupting
# "improvement". Everything below that is not 1Day writes to the INTRADAY table,
# whose key is (symbol, timestamp) with no record of the bar's size — so a
# 1-minute bar and a 15-minute bar collide four times an hour, the write is
# insert-or-ignore, and the survivor gets served back inside whichever series
# asks next, carrying the wrong high and low. Minute bars have their own tables
# (barcache.MinuteBar) and their own reader (qt.api.backtest.intraday_model_for);
# caching them here means teaching _plan and _closed_intraday about a second slot
# size first. Until then a 1-minute fetch passes straight through to Alpaca,
# which is correct, merely not thrifty — and it is only ever asked for over a
# window of hours.
CACHEABLE = ("1Day", "15Min")

# Daily bars are stored keyed by DAY, so their time-of-day is not preserved and a
# cached bar has to be re-stamped on read. The stamp is chosen to match Alpaca's
# own daily-bar convention closely enough that a cached bar behaves EXACTLY like a
# freshly-fetched one: same day bucket, and the same answer to any time-of-day
# rule (an entry-time window, say). Stocks: Alpaca stamps a daily bar at midnight
# ET (04:00/05:00Z) — pre-market, and the same ET day either way. Crypto: Alpaca
# stamps 00:00Z, which is also what the UTC-day bucketing expects. Do not "improve"
# these to mid-session times: that would silently start passing entry-window rules
# that a real daily bar fails.
DAILY_STAMP = {"stock": "T05:00:00Z", "crypto": "T00:00:00Z"}

# Coverage sanity. A cached series is trusted only when it looks CONTIGUOUS: no
# hole longer than this between consecutive bars, and a first bar close enough to
# the requested start. 5 calendar days clears a US long weekend plus a holiday
# (Thu close → Tue open) without letting a real hole through. Anything outside
# that and the symbol's whole window is re-fetched — wasteful, never wrong.
MAX_GAP_DAYS = 5
HEAD_TOLERANCE_DAYS = 5


def _parse(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _models(asset_class: str):
    """(daily model, intraday model) for this asset class — stocks and crypto
    live in separate tables because their 'day' means different things."""
    crypto = asset_class == "crypto"
    return (
        barcache.CryptoDailyBar if crypto else barcache.DailyBar,
        barcache.CryptoIntradayBar if crypto else barcache.IntradayBar,
    )


def _closed_daily(bars: list[dict], now: datetime | None = None) -> list[dict]:
    """Only the daily bars whose day has DEFINITIVELY closed: strictly before the
    current UTC day. Today's bar is still being written — its close moves until
    the session ends — and the cache is insert-or-ignore, so a partial row saved
    now would be served as gospel forever. Conservative for stocks too: once the
    UTC date has rolled past day D, the 16:00 ET close of day D is long past."""
    today = (now or datetime.now(timezone.utc)).strftime("%Y-%m-%d")
    return [b for b in bars if (b.get("t") or "")[:10] < today]


def _closed_intraday(bars: list[dict], now: datetime | None = None) -> list[dict]:
    """Only the 15-minute bars whose slot has DEFINITIVELY closed: strictly before
    the slot currently in progress. A bar is timestamped at the START of its slot,
    so the bar stamped one slot back finished exactly as the current slot began —
    that one is closed and safe; the current slot's is not."""
    now = now or datetime.now(timezone.utc)
    slot = now.replace(minute=(now.minute // 15) * 15, second=0, microsecond=0)
    out = []
    for b in bars:
        ts = b.get("t")
        if not ts:
            continue
        try:
            if _parse(ts) < slot:
                out.append(b)
        except ValueError:  # an unparseable stamp is not worth caching
            continue
    return out


def _trustworthy(bars: list[dict], start_day: str) -> bool:
    """Whether a cached series can be treated as COMPLETE up to its last bar.
    False when the head is missing (it starts well after the requested window) or
    a hole sits in the middle — either way we can't tell what's absent, so the
    caller re-fetches the whole window for that symbol rather than serve a series
    with pieces silently missing."""
    if not bars:
        return False
    try:
        stamps = [_parse(b["t"]) for b in bars]
    except (KeyError, ValueError):
        return False
    start = _parse(f"{start_day}T00:00:00Z")
    if stamps[0] - start > timedelta(days=HEAD_TOLERANCE_DAYS):
        return False
    return all(
        b - a <= timedelta(days=MAX_GAP_DAYS) for a, b in zip(stamps, stamps[1:])
    )


def _plan(
    cached: dict[str, list[dict]], symbols: list[str], timeframe: str,
    start_iso: str, now: datetime,
) -> tuple[dict[str, list[dict]], dict[str, str]]:
    """Work out, per symbol, what to keep from the cache and where the fetch has
    to start. Returns (bars to keep, {symbol: fetch start}) — a symbol absent from
    the second dict needs no fetch at all (the cache already reaches the newest
    CLOSED bar, so the only thing missing is the in-progress one)."""
    keep: dict[str, list[dict]] = {}
    fetch_from: dict[str, str] = {}
    start_day = start_iso[:10]
    for symbol in symbols:
        bars = cached.get(symbol) or []
        if not _trustworthy(bars, start_day):
            fetch_from[symbol] = start_iso  # whole window; nothing kept
            continue
        keep[symbol] = bars
        if timeframe == "1Day":
            last_day = (bars[-1]["t"] or "")[:10]
            next_day = (datetime.fromisoformat(last_day) + timedelta(days=1)).strftime("%Y-%m-%d")
            if next_day >= now.strftime("%Y-%m-%d"):
                continue  # only today's still-open bar is missing — don't fetch it
            fetch_from[symbol] = f"{next_day}T00:00:00Z"
        else:
            last = _parse(bars[-1]["t"])
            slot = now.replace(minute=(now.minute // 15) * 15, second=0, microsecond=0)
            if last + timedelta(minutes=15) >= slot:
                continue  # nothing but the in-progress slot is missing
            fetch_from[symbol] = _iso(last + timedelta(seconds=1))
    return keep, fetch_from


def _read_cache(
    symbols: list[str], asset_class: str, timeframe: str, start_iso: str
) -> dict[str, list[dict]]:
    """Everything the cache holds for these symbols from start_iso's day onward."""
    barcache.init_cache()
    daily_model, intraday_model = _models(asset_class)
    start_day = start_iso[:10]
    sess = barcache.session()
    try:
        if timeframe == "1Day":
            return barcache.cached_daily_bars(
                sess, symbols, start_day, model=daily_model,
                stamp=DAILY_STAMP.get(asset_class, DAILY_STAMP["stock"]),
            )
        return barcache.cached_intraday_bars(sess, symbols, start_day, model=intraday_model)
    finally:
        sess.close()


def _save(fetched: dict[str, list[dict]], asset_class: str, timeframe: str) -> None:
    """Persist the CLOSED bars from a fetch. Idempotent (insert-or-ignore), so a
    re-run over the same window writes nothing new."""
    daily_model, intraday_model = _models(asset_class)
    sess = barcache.session()
    try:
        for symbol, bars in fetched.items():
            if not bars:
                continue
            if timeframe == "1Day":
                barcache.save_daily_bars(sess, symbol, _closed_daily(bars), model=daily_model)
            else:
                barcache.save_intraday_bars(sess, symbol, _closed_intraday(bars), model=intraday_model)
        sess.commit()
    finally:
        sess.close()


def _merge(kept: list[dict], fetched: list[dict]) -> list[dict]:
    """Cached bars followed by freshly fetched ones, de-duped by timestamp (the
    cached copy wins) and re-sorted — belt and braces against an overlapping
    edge, since Alpaca's `start` is inclusive."""
    by_ts: dict[str, dict] = {}
    for b in fetched:
        if b.get("t"):
            by_ts[b["t"]] = b
    for b in kept:
        if b.get("t"):
            by_ts[b["t"]] = b
    return [by_ts[t] for t in sorted(by_ts)]


async def fetch_bars(
    client,
    symbols: list[str],
    asset_class: str,
    timeframe: str,
    start_iso: str,
    *,
    use_cache: bool = True,
) -> dict[str, list[dict]]:
    """Bars for `symbols` from `start_iso`, served read-through the bar cache.

    Same signature and same return shape as AlpacaClient.historical_bars, so it
    is a drop-in replacement at the call site. Any cache trouble at all — not
    configured, unreachable, mid-fetch failure — logs and falls back to a plain
    Alpaca fetch, because a backtest failing over a cache is never acceptable."""
    if not symbols:
        return {}
    if not use_cache or timeframe not in CACHEABLE:
        return await client.historical_bars(symbols, asset_class, timeframe, start_iso)

    try:
        now = datetime.now(timezone.utc)
        cached = _read_cache(symbols, asset_class, timeframe, start_iso)
        kept, fetch_from = _plan(cached, symbols, timeframe, start_iso, now)
    except Exception:  # noqa: BLE001 — the cache is an optimization, never a gate
        log.warning("bar cache unavailable — fetching %s from Alpaca", timeframe, exc_info=True)
        return await client.historical_bars(symbols, asset_class, timeframe, start_iso)

    # Batch the download by shared start so symbols needing the same slice ride
    # one request (the common case: everything wants the same recent edge).
    by_start: dict[str, list[str]] = {}
    for symbol, start in fetch_from.items():
        by_start.setdefault(start, []).append(symbol)
    fetched: dict[str, list[dict]] = {}
    for start, syms in sorted(by_start.items()):
        fetched.update(await client.historical_bars(sorted(syms), asset_class, timeframe, start))

    if fetched:
        try:
            _save(fetched, asset_class, timeframe)
        except Exception:  # noqa: BLE001 — we already HAVE the bars; saving is a bonus
            log.warning("could not write %s bars to the bar cache", timeframe, exc_info=True)

    hits = sum(1 for s in symbols if s not in fetch_from)
    log.info(
        "bar cache: %s/%s symbols served fully from cache (%s, %s)",
        hits, len(symbols), timeframe, asset_class,
    )
    return {s: _merge(kept.get(s) or [], fetched.get(s) or []) for s in symbols}
