"""Historical bar + daily-movers cache.

Bulk, disposable, rebuildable data kept OUT of qt.db (which holds precious
config/keys/journal and is backed up). The backend is configurable via
`QT_BAR_CACHE_URL` (see qt.paths.bar_cache_url): defaults to a local `bars.db`
SQLite file; point it at Postgres for a durable, shared cache.

This module is the FOUNDATION for the "scanner replay" backtest: it stores
daily bars for a broad universe, and reconstructs each past day's "today's
risers" (top-N by % gain, after the same price/volume filters the live
scanner applies) — because Alpaca has no historical movers endpoint, so the
risers must be recomputed from price history. `rank_movers` is a pure,
broker-free function so the reconstruction is exhaustively testable.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import Float, Integer, String, create_engine, func
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker
from sqlalchemy.orm import Session as OrmSession

from qt.paths import bar_cache_url

log = logging.getLogger("qt.barcache")


class CacheBase(DeclarativeBase):
    pass


class DailyBar(CacheBase):
    """One symbol's daily OHLCV bar (immutable once the day closes)."""

    __tablename__ = "daily_bars"

    symbol: Mapped[str] = mapped_column(String(32), primary_key=True)
    day: Mapped[str] = mapped_column(String(10), primary_key=True)  # YYYY-MM-DD (UTC session date)
    o: Mapped[float] = mapped_column(Float, default=0.0)
    h: Mapped[float] = mapped_column(Float, default=0.0)
    l: Mapped[float] = mapped_column(Float, default=0.0)
    c: Mapped[float] = mapped_column(Float, default=0.0)
    v: Mapped[float] = mapped_column(Float, default=0.0)
    vw: Mapped[float | None] = mapped_column(Float, nullable=True)


class DailyMover(CacheBase):
    """A reconstructed 'today's risers' entry: rank N of a past day."""

    __tablename__ = "daily_movers"

    day: Mapped[str] = mapped_column(String(10), primary_key=True)
    rank: Mapped[int] = mapped_column(Integer, primary_key=True)
    symbol: Mapped[str] = mapped_column(String(32))
    change_pct: Mapped[float] = mapped_column(Float)
    price: Mapped[float] = mapped_column(Float)
    dollar_volume: Mapped[float] = mapped_column(Float)


class IntradayBar(CacheBase):
    """One intraday bar (e.g. 15-min) for a mover, so the scanner-replay
    backtest can judge an intraday strategy on how the day actually unfolded —
    entries after the open, VWAP, and flatten-before-close all behave for real.
    Pulled only for the reconstructed movers, so this table stays bounded."""

    __tablename__ = "intraday_bars"

    symbol: Mapped[str] = mapped_column(String(32), primary_key=True)
    ts: Mapped[str] = mapped_column(String(20), primary_key=True)  # ISO 'YYYY-MM-DDTHH:MM:SSZ'
    o: Mapped[float] = mapped_column(Float, default=0.0)
    h: Mapped[float] = mapped_column(Float, default=0.0)
    l: Mapped[float] = mapped_column(Float, default=0.0)
    c: Mapped[float] = mapped_column(Float, default=0.0)
    v: Mapped[float] = mapped_column(Float, default=0.0)
    vw: Mapped[float | None] = mapped_column(Float, nullable=True)


# ---------------------------------------------------------------------------
# CRYPTO cache — SEPARATE tables with the same column shapes as the stock ones.
#
# Kept apart from the stock tables on purpose:
#   * create_all ADDS these without touching the existing (large, expensive)
#     stock tables — fully non-destructive.
#   * a shared daily_movers table would collide on its (day, rank) PK the moment
#     both a stock and a crypto riser wanted rank 1 of the same day.
#
# The one semantic difference from stocks: crypto's "day" is the UTC calendar
# day (Alpaca's crypto daily bar is UTC-aligned and the market is 24/7), not the
# ET session day. The columns are identical, so the shared read/write/reconstruct
# helpers below are parameterized by model rather than duplicated.
# ---------------------------------------------------------------------------


class CryptoDailyBar(CacheBase):
    """One crypto pair's daily OHLCV bar, keyed by UTC calendar day."""

    __tablename__ = "crypto_daily_bars"

    symbol: Mapped[str] = mapped_column(String(32), primary_key=True)
    day: Mapped[str] = mapped_column(String(10), primary_key=True)  # YYYY-MM-DD (UTC day)
    o: Mapped[float] = mapped_column(Float, default=0.0)
    h: Mapped[float] = mapped_column(Float, default=0.0)
    l: Mapped[float] = mapped_column(Float, default=0.0)
    c: Mapped[float] = mapped_column(Float, default=0.0)
    v: Mapped[float] = mapped_column(Float, default=0.0)
    vw: Mapped[float | None] = mapped_column(Float, nullable=True)


class CryptoDailyMover(CacheBase):
    """A reconstructed crypto 'today's risers' entry: rank N of a past UTC day."""

    __tablename__ = "crypto_daily_movers"

    day: Mapped[str] = mapped_column(String(10), primary_key=True)
    rank: Mapped[int] = mapped_column(Integer, primary_key=True)
    symbol: Mapped[str] = mapped_column(String(32))
    change_pct: Mapped[float] = mapped_column(Float)
    price: Mapped[float] = mapped_column(Float)
    dollar_volume: Mapped[float] = mapped_column(Float)


class CryptoIntradayBar(CacheBase):
    """One intraday (e.g. 15-min) crypto bar for a mover, timestamps in real UTC."""

    __tablename__ = "crypto_intraday_bars"

    symbol: Mapped[str] = mapped_column(String(32), primary_key=True)
    ts: Mapped[str] = mapped_column(String(20), primary_key=True)  # ISO 'YYYY-MM-DDTHH:MM:SSZ'
    o: Mapped[float] = mapped_column(Float, default=0.0)
    h: Mapped[float] = mapped_column(Float, default=0.0)
    l: Mapped[float] = mapped_column(Float, default=0.0)
    c: Mapped[float] = mapped_column(Float, default=0.0)
    v: Mapped[float] = mapped_column(Float, default=0.0)
    vw: Mapped[float | None] = mapped_column(Float, nullable=True)


# ---------------------------------------------------------------------------
# ONE-MINUTE bars — SEPARATE tables again, for a reason that is not stylistic.
#
# The intraday tables are keyed (symbol, ts) with NO record of the bar's size,
# and a 15-minute bar and a 1-minute bar share a timestamp four times an hour
# (13:15, 13:30, …). Writing both into one table would therefore:
#
#   * silently DROP the 1-minute bar wherever a 15-minute one already exists —
#     the writes are insert-or-ignore, because historical bars are immutable;
#   * and, worse, serve the survivor back inside a 1-minute series, so a bar
#     labelled 13:15 would carry the high and low of the whole quarter hour.
#     Stops are checked against those extremes, so the replay would trigger on a
#     range that never happened inside that minute — a wrong answer that looks
#     entirely normal.
#
# Adding the size to the primary key would fix the collision and cannot be done:
# `init_cache` only ever CREATES missing tables (no migrations, by design), so
# the change would land only after dropping the existing stock intraday table,
# which is the largest and most expensive thing in this cache.
#
# Separate tables are what the crypto tables already do, and for the same reason:
# create_all adds them without touching anything that exists.
#
# WHY THEY EXIST AT ALL: the live engine evaluates every 60 seconds, so a replay
# on 15-minute bars cannot resolve a decision it made between two of them — a
# signal that appeared and vanished inside the quarter hour is invisible, and the
# fidelity report reads that as "the replay missed a trade". These bars are
# fetched only for a SHORT fidelity window (see MAX_HOURS_FOR_MINUTE_REPLAY);
# ordinary backtests never touch them, because 1,440 bars per pair per day over a
# 180-day window is millions of rows to answer a question 15-minute bars already
# answer well enough.
# ---------------------------------------------------------------------------


class MinuteBar(CacheBase):
    """One 1-minute stock bar. Same shape as IntradayBar; see the note above for
    why it is not the same TABLE."""

    __tablename__ = "minute_bars"

    symbol: Mapped[str] = mapped_column(String(32), primary_key=True)
    ts: Mapped[str] = mapped_column(String(20), primary_key=True)  # ISO 'YYYY-MM-DDTHH:MM:SSZ'
    o: Mapped[float] = mapped_column(Float, default=0.0)
    h: Mapped[float] = mapped_column(Float, default=0.0)
    l: Mapped[float] = mapped_column(Float, default=0.0)
    c: Mapped[float] = mapped_column(Float, default=0.0)
    v: Mapped[float] = mapped_column(Float, default=0.0)
    vw: Mapped[float | None] = mapped_column(Float, nullable=True)


class CryptoMinuteBar(CacheBase):
    """One 1-minute crypto bar, timestamps in real UTC."""

    __tablename__ = "crypto_minute_bars"

    symbol: Mapped[str] = mapped_column(String(32), primary_key=True)
    ts: Mapped[str] = mapped_column(String(20), primary_key=True)
    o: Mapped[float] = mapped_column(Float, default=0.0)
    h: Mapped[float] = mapped_column(Float, default=0.0)
    l: Mapped[float] = mapped_column(Float, default=0.0)
    c: Mapped[float] = mapped_column(Float, default=0.0)
    v: Mapped[float] = mapped_column(Float, default=0.0)
    vw: Mapped[float | None] = mapped_column(Float, nullable=True)


def make_engine(url: str | None = None) -> Engine:
    url = url or bar_cache_url()
    kwargs: dict = {}
    if url.startswith("sqlite"):
        kwargs["connect_args"] = {"check_same_thread": False}
    return create_engine(url, **kwargs)


_engine: Engine | None = None
_Session: sessionmaker | None = None


def _prod() -> sessionmaker:
    global _engine, _Session
    if _Session is None:
        _engine = make_engine()
        _Session = sessionmaker(bind=_engine, expire_on_commit=False)
    return _Session


def init_cache(engine: Engine | None = None) -> None:
    """Create the cache tables if absent. No migrations — the cache is
    disposable; a schema change is handled by dropping/recreating it."""
    if engine is None:
        _prod()  # lazily builds the global engine
        engine = _engine
    CacheBase.metadata.create_all(engine)


def session() -> OrmSession:
    return _prod()()


# ---------------------------------------------------------------------------
# Reconstruction — the pure core (no DB, no broker).
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DayQuote:
    """One symbol's state on a given day: its close, the prior session's close
    (for the % move), the day's volume, and (optionally) VWAP."""

    symbol: str
    close: float
    prev_close: float
    volume: float
    vwap: float | None = None
    high: float | None = None  # intraday peak; when set, ranking uses the peak gain


def _norm_symbol(symbol: str) -> str:
    """Match symbols across the two spellings in play: the scanner config holds
    'TRUMP/USD' (what you typed), the bar cache holds whatever Alpaca's bars
    endpoint returned, which for crypto is slash-less. Without this an exclusion
    silently does nothing."""
    return symbol.replace("/", "").strip().upper()


def rank_movers(
    quotes: list[DayQuote],
    top_n: int,
    *,
    min_change_pct: float = 0.0,
    max_change_pct: float = 0.0,
    min_price: float = 0.0,
    max_price: float = 0.0,
    min_dollar_volume: float = 0.0,
    exclude: set[str] | None = None,
) -> list[tuple[str, float, float, float]]:
    """Reconstruct a day's 'today's risers': the top-N symbols by % gain that
    clear the scanner's filters. Returns (symbol, change_pct, price, $ volume),
    ranked highest-gain first. Mirrors the live scanner's filter order so a
    replay matches what the scanner would have surfaced that day."""
    ranked: list[tuple[str, float, float, float]] = []
    banned = {_norm_symbol(s) for s in (exclude or set())}
    for q in quotes:
        if not q.prev_close or not q.close:
            continue
        # Also filtered on the way IN, so a sweep stops spending rows on names
        # you've banned. Read-time filtering is what fixes an EXISTING cache.
        if banned and _norm_symbol(q.symbol) in banned:
            continue
        # Rank on the intraday PEAK (daily high) when we have it: an intraday
        # scanner flags a stock that spiked +40% at 10am even if it closed flat,
        # and those pump-and-fade names are exactly what an intraday strategy
        # trades. Ranking on the close would silently drop them.
        ref = q.high if q.high is not None else q.close
        change = (ref / q.prev_close - 1) * 100
        dollar_volume = q.volume * (q.vwap or q.close)
        if change < min_change_pct:
            continue
        if max_change_pct and change > max_change_pct:
            continue
        if min_price and q.close < min_price:
            continue
        if max_price and q.close > max_price:
            continue
        if min_dollar_volume and dollar_volume < min_dollar_volume:
            continue
        ranked.append((q.symbol, round(change, 2), round(q.close, 4), round(dollar_volume)))
    ranked.sort(key=lambda r: r[1], reverse=True)
    return ranked[:top_n]


# ---------------------------------------------------------------------------
# Cache read/write (portable across SQLite and Postgres).
# ---------------------------------------------------------------------------


# How many rows go into one INSERT. A bar row binds 8 parameters, and BOTH
# engines cap the parameters in a single statement — SQLite at 32,766 and
# Postgres at 65,535 — so a single VALUES list is not something that can be
# allowed to grow with the caller's appetite. It never did before 1-minute bars:
# a day of 15-minute bars is 96 rows for a crypto pair, and the fetch is per day.
# A minute fill covers a contiguous RUN of days in one call, so four days of one
# crypto pair is 5,760 rows — 46,080 parameters, past SQLite outright, and eight
# days would be past Postgres too. Chunking costs one extra round trip per 1,000
# rows and removes the ceiling entirely.
INSERT_CHUNK_ROWS = 1000


def _insert_ignore(sess: OrmSession, model, rows: list[dict]) -> None:
    """Bulk insert, skipping rows that already exist. Historical bars are
    immutable, so 'do nothing' on a primary-key conflict is correct and fast."""
    if not rows:
        return
    ins = pg_insert if sess.bind.dialect.name == "postgresql" else sqlite_insert
    for i in range(0, len(rows), INSERT_CHUNK_ROWS):
        sess.execute(ins(model).on_conflict_do_nothing().values(rows[i : i + INSERT_CHUNK_ROWS]))


def save_daily_bars(sess: OrmSession, symbol: str, bars: list[dict], model=DailyBar) -> int:
    """Persist Alpaca daily bars (dicts with t,o,h,l,c,v,vw) for one symbol.
    Idempotent — re-running a sweep won't duplicate closed bars. `model` selects
    the stock (default) or crypto daily table; the row shape is identical."""
    rows: list[dict] = []
    for b in bars:
        day = (b.get("t") or "")[:10]
        if not day:
            continue
        rows.append(
            {
                "symbol": symbol, "day": day,
                "o": float(b.get("o") or 0), "h": float(b.get("h") or 0),
                "l": float(b.get("l") or 0), "c": float(b.get("c") or 0),
                "v": float(b.get("v") or 0),
                "vw": float(b["vw"]) if b.get("vw") is not None else None,
            }
        )
    _insert_ignore(sess, model, rows)
    return len(rows)


def store_movers(
    sess: OrmSession, day: str, ranked: list[tuple[str, float, float, float]], model=DailyMover
) -> None:
    """Replace the cached top-N risers for one day (stock or crypto table)."""
    sess.query(model).filter(model.day == day).delete()
    for rank, (symbol, change_pct, price, dollar_volume) in enumerate(ranked, start=1):
        sess.add(model(day=day, rank=rank, symbol=symbol, change_pct=change_pct,
                       price=price, dollar_volume=dollar_volume))


def top_movers(sess: OrmSession, day: str, model=DailyMover) -> list:
    """The cached risers for a day, best first (stock or crypto table)."""
    return sess.query(model).filter(model.day == day).order_by(model.rank).all()


def movers_between(
    sess: OrmSession,
    start_day: str,
    top_n: int | None = None,
    model=DailyMover,
    *,
    exclude: set[str] | None = None,
    min_price: float = 0.0,
    max_price: float = 0.0,
    min_dollar_volume: float = 0.0,
    end_day: str | None = None,
) -> dict[str, list[str]]:
    """{day: [symbols ranked]} for all reconstructed days on/after start_day —
    the per-day 'today's risers' a scanner-replay backtest gates entries on.

    `end_day` (inclusive) closes the other side, for a replay of a window that
    ended in the past rather than one running up to today. None = everything from
    start_day on, which is what a plain "last N days" backtest wants.

    `top_n` narrows each day to its best N at READ time: the cache stores a
    generous set (see barsweep.SWEEP_STORE_TOP_N), so the backtest can dial the
    riser count up or down instantly without re-sweeping or re-ranking. None
    returns everything stored.

    THE FILTERS ARE RE-APPLIED HERE FOR THE SAME REASON. A mover list is frozen
    at sweep time, so a symbol added to "never trade these" — or a price floor
    raised afterwards — went on being replayed regardless: the backtest traded
    names the live engine is configured never to touch, and reported a result for
    a strategy you could not actually run. Every filter is re-checked against the
    row's stored price and volume, so changing a scanner setting takes effect on
    the next backtest with no re-sweep.

    The exclude list is matched loosely on purpose: the cache stores 'TRUMPUSD'
    where the scanner config holds 'TRUMP/USD'."""
    query = sess.query(model).filter(model.day >= start_day)
    if end_day is not None:
        query = query.filter(model.day <= end_day)
    rows = query.order_by(model.day, model.rank).all()
    banned = {_norm_symbol(s) for s in (exclude or set())}
    out: dict[str, list[str]] = {}
    for m in rows:  # rows are rank-ordered, so appending while under the cap keeps the top N
        if banned and _norm_symbol(m.symbol) in banned:
            continue
        if min_price and m.price < min_price:
            continue
        if max_price and m.price > max_price:
            continue
        if min_dollar_volume and m.dollar_volume < min_dollar_volume:
            continue
        lst = out.setdefault(m.day, [])
        if top_n is None or len(lst) < top_n:
            lst.append(m.symbol)
    return out


def cached_daily_bars(
    sess: OrmSession, symbols: list[str], start_day: str, model=DailyBar, stamp: str = "T14:00:00Z",
    *, end_day: str | None = None,
) -> dict[str, list[dict]]:
    """Cached daily bars for `symbols` on/after start_day, shaped like Alpaca
    bar dicts (t/o/h/l/c/v/vw) so the backtester consumes them unchanged.

    `end_day` (inclusive) bounds the far side for a replay of a past window.

    `stamp` places each bar INSIDE its trading day so the backtest's day-bucket
    matches the movers key. Stocks stamp 14:00Z (10:00 ET — inside the ET day,
    where midnight UTC would roll back to the prior calendar day). Crypto stamps
    12:00Z — squarely inside the UTC calendar day the crypto backtest buckets by."""
    if not symbols:
        return {}
    out: dict[str, list[dict]] = {}
    # Chunk the IN() list so a large movers union doesn't build a giant query.
    for i in range(0, len(symbols), 500):
        chunk = symbols[i : i + 500]
        query = sess.query(model).filter(model.symbol.in_(chunk), model.day >= start_day)
        if end_day is not None:
            query = query.filter(model.day <= end_day)
        rows = query.order_by(model.symbol, model.day).all()
        for b in rows:
            out.setdefault(b.symbol, []).append(
                {"t": f"{b.day}{stamp}", "o": b.o, "h": b.h, "l": b.l, "c": b.c, "v": b.v, "vw": b.vw}
            )
    return out


def save_intraday_bars(sess: OrmSession, symbol: str, bars: list[dict], model=IntradayBar) -> int:
    """Persist Alpaca intraday bars (dicts with t,o,h,l,c,v,vw) for one symbol.
    Idempotent — closed intraday bars are immutable, so re-sweeps don't duplicate.
    `model` selects the stock (default) or crypto intraday table."""
    rows: list[dict] = []
    for b in bars:
        ts = b.get("t")
        if not ts:
            continue
        rows.append(
            {
                "symbol": symbol, "ts": ts,
                "o": float(b.get("o") or 0), "h": float(b.get("h") or 0),
                "l": float(b.get("l") or 0), "c": float(b.get("c") or 0),
                "v": float(b.get("v") or 0),
                "vw": float(b["vw"]) if b.get("vw") is not None else None,
            }
        )
    _insert_ignore(sess, model, rows)
    return len(rows)


def cached_intraday_bars(
    sess: OrmSession, symbols: list[str], start_day: str, model=IntradayBar,
    *, end_day: str | None = None,
) -> dict[str, list[dict]]:
    """Cached intraday bars for `symbols` with a timestamp on/after start_day,
    shaped like Alpaca bar dicts (t/o/h/l/c/v/vw) so the backtester consumes
    them unchanged. ISO timestamps sort/compare lexically, so a plain string
    filter on the 'YYYY-MM-DD' prefix is correct.

    `end_day` is an INCLUSIVE day, so the bound is the day after it, exclusive —
    every stamp within end_day ('...T19:45:00Z' and all the rest) sorts before
    the next day's midnight. Comparing against end_day itself would keep only the
    bars stamped exactly at midnight, i.e. none of them."""
    if not symbols:
        return {}
    end_before = (
        (date.fromisoformat(end_day) + timedelta(days=1)).isoformat()
        if end_day is not None
        else None
    )
    out: dict[str, list[dict]] = {}
    for i in range(0, len(symbols), 500):
        chunk = symbols[i : i + 500]
        query = sess.query(model).filter(model.symbol.in_(chunk), model.ts >= start_day)
        if end_before is not None:
            query = query.filter(model.ts < end_before)
        rows = query.order_by(model.symbol, model.ts).all()
        for b in rows:
            out.setdefault(b.symbol, []).append(
                {"t": b.ts, "o": b.o, "h": b.h, "l": b.l, "c": b.c, "v": b.v, "vw": b.vw}
            )
    return out


def cached_intraday_days(
    sess: OrmSession, symbols: list[str], start_day: str, model=IntradayBar,
    *, end_day: str | None = None,
) -> dict[str, set[str]]:
    """{symbol: {'YYYY-MM-DD', …}} — which DAYS each symbol already has intraday
    bars for, without loading the bars themselves.

    A replay that wants to fill its own gaps needs to know what is missing, and
    reading every bar to find out is the expensive way round: a single crypto
    pair is ~96 rows a day. This is one DISTINCT over the timestamp prefix, so
    the answer costs the same whether the cache holds a day or a year.

    Same inclusive `end_day` convention as cached_intraday_bars — see the note
    there about why the bound is the day AFTER it, exclusive."""
    if not symbols:
        return {}
    end_before = (
        (date.fromisoformat(end_day) + timedelta(days=1)).isoformat()
        if end_day is not None
        else None
    )
    out: dict[str, set[str]] = {}
    day_col = func.substr(model.ts, 1, 10)
    for i in range(0, len(symbols), 500):
        chunk = symbols[i : i + 500]
        query = sess.query(model.symbol, day_col).filter(
            model.symbol.in_(chunk), model.ts >= start_day
        )
        if end_before is not None:
            query = query.filter(model.ts < end_before)
        for symbol, day in query.distinct().all():
            out.setdefault(symbol, set()).add(day)
    return out


def latest_daily_day(sess: OrmSession, model=DailyBar) -> str | None:
    """The newest day the DAILY cache holds, or None when it is empty.

    Read as "how far the sweep has got". Days after it are not gaps in the
    cache — they are days the sweep could not have covered yet, which for crypto
    includes the current UTC day (its daily bar is still forming, so
    sweep_crypto_daily_bars deliberately refuses to store it). A replay whose
    window runs into one of those days has to fetch its own bars or see
    nothing."""
    return sess.query(func.max(model.day)).scalar()


def has_intraday(sess: OrmSession, model=IntradayBar) -> bool:
    """Whether any intraday bars are cached (stage-2 replay is possible)."""
    return sess.query(model).first() is not None


def freshest_mover(sess: OrmSession, mover_model=DailyMover, intraday_model=IntradayBar) -> dict | None:
    """The #1 riser on the most recent reconstructed day, and whether its 15-min
    bars are cached — a quick 'is the intraday sweep caught up to the latest
    movers?' spot-check for the UI. None if no movers are cached yet."""
    row = (
        sess.query(mover_model)
        .order_by(mover_model.day.desc(), mover_model.rank.asc())
        .first()
    )
    if row is None:
        return None
    has_15m = (
        sess.query(intraday_model)
        .filter(intraday_model.symbol == row.symbol, intraday_model.ts.like(f"{row.day}T%"))
        .first()
        is not None
    )
    return {
        "symbol": row.symbol,
        "day": row.day,
        "change_pct": row.change_pct,
        "has_intraday": has_15m,
    }


# How far back intraday bars are kept. They are the only table that grows
# without bound: a daily bar is one row per symbol per day, but a 15-minute bar
# is ~26 rows a day for a stock and ~96 for a crypto pair, for every mover the
# backtester ever asked about. Left alone, a cache that serves an active user
# grows forever and nothing ever reclaims it.
#
# The window is deliberately the LONGEST backtest the app will accept (730 days
# — see BacktestBody), not a tighter number. Pruning a bar something can still
# ask for only trades disk for a re-download later, which is churn, not saving.
# At 730 nothing the app is capable of requesting is ever thrown away, and the
# table still cannot grow forever: everything past the point where no backtest
# can reach goes.
# QT_BAR_CACHE_KEEP_DAYS overrides it — lower if disk is tight and you never
# replay that far back, 0 to keep everything forever.
INTRADAY_KEEP_DAYS = 730

# ONE-MINUTE bars get their own, far shorter window, and it is not a matter of
# taste: a minute bar is fifteen rows where a 15-minute bar is one — ~1,440 a day
# for a crypto pair, ~390 for a stock session — and they are fetched for exactly
# one purpose, grading a fidelity window that is hours long and days old. Nothing
# in the app can ask for a minute bar older than the newest comparison anyone is
# still interested in, so keeping them for two years would be paying 15x the disk
# for history no code path reads. A month is generous for "re-run last week's
# comparison" and still bounds the table at a small multiple of the 15-minute one.
# QT_BAR_CACHE_MINUTE_KEEP_DAYS overrides it; 0 keeps them forever.
MINUTE_KEEP_DAYS = 30


def _keep_days(var: str, default: int) -> int:
    """A retention window read from the environment. Invalid values fall back to
    the default rather than disabling the prune — a typo in an env var should
    not silently be the one setting that lets the disk fill up."""
    raw = os.environ.get(var)
    if raw is None or not raw.strip():
        return default
    try:
        days = int(raw)
    except ValueError:
        log.warning("%s=%r is not a number — keeping the default", var, raw)
        return default
    return max(0, days)


def intraday_keep_days() -> int:
    return _keep_days("QT_BAR_CACHE_KEEP_DAYS", INTRADAY_KEEP_DAYS)


def minute_keep_days() -> int:
    return _keep_days("QT_BAR_CACHE_MINUTE_KEEP_DAYS", MINUTE_KEEP_DAYS)


def prune_intraday(
    sess: OrmSession, keep_days: int | None = None, minute_keep: int | None = None
) -> dict:
    """Delete intraday bars older than the retention window, both markets — and
    1-minute bars older than THEIR (much shorter) window, also both markets.

    The two windows are separate switches. Turning one off keeps that table
    forever and leaves the other pruning normally, which is the combination that
    actually matters: minute bars are the ones that grow fastest and are wanted
    least, so a user with a big cache disk who wants to keep 15-minute history
    forever should not thereby inherit two years of minute bars as well.

    Daily bars are deliberately NOT pruned. They are cheap (one row per symbol
    per day) and they are what every past day's movers list is reconstructed
    from — dropping them would not reclaim much and would quietly shorten the
    history a scanner replay can cover.

    `ts` is an ISO-8601 UTC string, so a lexical comparison IS a chronological
    one, and the delete works identically on SQLite and Postgres."""
    days = intraday_keep_days() if keep_days is None else keep_days
    minute_days = minute_keep_days() if minute_keep is None else minute_keep

    def cutoff_for(window: int) -> str:
        return (datetime.now(timezone.utc) - timedelta(days=window)).strftime("%Y-%m-%dT%H:%M:%SZ")

    out: dict = {
        # True when ANY table was pruned, so the historical meaning ("did this
        # run reclaim anything at all?") still holds for a caller reading it.
        "pruned": days > 0 or minute_days > 0,
        "keep_days": days,
        "minute_keep_days": minute_days,
        "stock": 0, "crypto": 0, "stock_minute": 0, "crypto_minute": 0,
    }
    if days > 0:
        cutoff = cutoff_for(days)
        out["cutoff"] = cutoff
        for key, model in (("stock", IntradayBar), ("crypto", CryptoIntradayBar)):
            out[key] = int(
                sess.query(model).filter(model.ts < cutoff).delete(synchronize_session=False)
            )
    if minute_days > 0:
        minute_cutoff = cutoff_for(minute_days)
        out["minute_cutoff"] = minute_cutoff
        for key, model in (("stock_minute", MinuteBar), ("crypto_minute", CryptoMinuteBar)):
            out[key] = int(
                sess.query(model).filter(model.ts < minute_cutoff).delete(synchronize_session=False)
            )
    sess.commit()
    return out


def cache_stats(
    sess: OrmSession, daily_model=DailyBar, mover_model=DailyMover, intraday_model=IntradayBar
) -> dict:
    """What's actually PERSISTED in the cache, independent of any in-process
    sweep counters (which reset on redeploy). Lets the UI show the real state of
    a durable Postgres cache after a container restart instead of zeros. Defaults
    to the stock tables; pass the crypto models for the crypto cache view."""
    return {
        "daily_symbols": int(sess.query(func.count(func.distinct(daily_model.symbol))).scalar() or 0),
        "movers_days": int(sess.query(func.count(func.distinct(mover_model.day))).scalar() or 0),
        "intraday_bars": int(sess.query(func.count()).select_from(intraday_model).scalar() or 0),
        "latest_day": sess.query(func.max(daily_model.day)).scalar(),  # 'YYYY-MM-DD' | None
        "freshest_mover": freshest_mover(sess, mover_model, intraday_model),
    }


def crypto_cache_stats(sess: OrmSession) -> dict:
    """cache_stats over the crypto tables."""
    return cache_stats(sess, CryptoDailyBar, CryptoDailyMover, CryptoIntradayBar)
