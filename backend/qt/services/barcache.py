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

from dataclasses import dataclass
from datetime import date, timedelta

from sqlalchemy import Float, Integer, String, create_engine, func
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker
from sqlalchemy.orm import Session as OrmSession

from qt.paths import bar_cache_url


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


def _insert_ignore(sess: OrmSession, model, rows: list[dict]) -> None:
    """Bulk insert, skipping rows that already exist. Historical bars are
    immutable, so 'do nothing' on a primary-key conflict is correct and fast."""
    if not rows:
        return
    ins = pg_insert if sess.bind.dialect.name == "postgresql" else sqlite_insert
    sess.execute(ins(model).on_conflict_do_nothing().values(rows))


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
