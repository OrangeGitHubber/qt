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

from sqlalchemy import Float, Integer, String, create_engine
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


def rank_movers(
    quotes: list[DayQuote],
    top_n: int,
    *,
    min_change_pct: float = 0.0,
    max_change_pct: float = 0.0,
    min_price: float = 0.0,
    max_price: float = 0.0,
    min_dollar_volume: float = 0.0,
) -> list[tuple[str, float, float, float]]:
    """Reconstruct a day's 'today's risers': the top-N symbols by % gain that
    clear the scanner's filters. Returns (symbol, change_pct, price, $ volume),
    ranked highest-gain first. Mirrors the live scanner's filter order so a
    replay matches what the scanner would have surfaced that day."""
    ranked: list[tuple[str, float, float, float]] = []
    for q in quotes:
        if not q.prev_close or not q.close:
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


def save_daily_bars(sess: OrmSession, symbol: str, bars: list[dict]) -> int:
    """Persist Alpaca daily bars (dicts with t,o,h,l,c,v,vw) for one symbol.
    Idempotent — re-running a sweep won't duplicate closed bars."""
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
    _insert_ignore(sess, DailyBar, rows)
    return len(rows)


def store_movers(sess: OrmSession, day: str, ranked: list[tuple[str, float, float, float]]) -> None:
    """Replace the cached top-N risers for one day."""
    sess.query(DailyMover).filter(DailyMover.day == day).delete()
    for rank, (symbol, change_pct, price, dollar_volume) in enumerate(ranked, start=1):
        sess.add(DailyMover(day=day, rank=rank, symbol=symbol, change_pct=change_pct,
                            price=price, dollar_volume=dollar_volume))


def top_movers(sess: OrmSession, day: str) -> list[DailyMover]:
    """The cached risers for a day, best first."""
    return sess.query(DailyMover).filter(DailyMover.day == day).order_by(DailyMover.rank).all()


def movers_between(
    sess: OrmSession, start_day: str, top_n: int | None = None
) -> dict[str, list[str]]:
    """{day: [symbols ranked]} for all reconstructed days on/after start_day —
    the per-day 'today's risers' a scanner-replay backtest gates entries on.

    `top_n` narrows each day to its best N at READ time: the cache stores a
    generous set (see barsweep.SWEEP_STORE_TOP_N), so the backtest can dial the
    riser count up or down instantly without re-sweeping or re-ranking. None
    returns everything stored."""
    rows = (
        sess.query(DailyMover)
        .filter(DailyMover.day >= start_day)
        .order_by(DailyMover.day, DailyMover.rank)
        .all()
    )
    out: dict[str, list[str]] = {}
    for m in rows:  # rows are rank-ordered, so appending while under the cap keeps the top N
        lst = out.setdefault(m.day, [])
        if top_n is None or len(lst) < top_n:
            lst.append(m.symbol)
    return out


def cached_daily_bars(sess: OrmSession, symbols: list[str], start_day: str) -> dict[str, list[dict]]:
    """Cached daily bars for `symbols` on/after start_day, shaped like Alpaca
    bar dicts (t/o/h/l/c/v/vw) so the backtester consumes them unchanged."""
    if not symbols:
        return {}
    out: dict[str, list[dict]] = {}
    # Chunk the IN() list so a large movers union doesn't build a giant query.
    for i in range(0, len(symbols), 500):
        chunk = symbols[i : i + 500]
        rows = (
            sess.query(DailyBar)
            .filter(DailyBar.symbol.in_(chunk), DailyBar.day >= start_day)
            .order_by(DailyBar.symbol, DailyBar.day)
            .all()
        )
        for b in rows:
            # Stamp at 14:00Z (10:00 ET) — INSIDE the ET trading day — not midnight
            # UTC, which _et_day() would roll back to the previous calendar day and
            # so misalign the backtest's day key from the movers/eligible-by-day key.
            out.setdefault(b.symbol, []).append(
                {"t": f"{b.day}T14:00:00Z", "o": b.o, "h": b.h, "l": b.l, "c": b.c, "v": b.v, "vw": b.vw}
            )
    return out


def save_intraday_bars(sess: OrmSession, symbol: str, bars: list[dict]) -> int:
    """Persist Alpaca intraday bars (dicts with t,o,h,l,c,v,vw) for one symbol.
    Idempotent — closed intraday bars are immutable, so re-sweeps don't duplicate."""
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
    _insert_ignore(sess, IntradayBar, rows)
    return len(rows)


def cached_intraday_bars(
    sess: OrmSession, symbols: list[str], start_day: str
) -> dict[str, list[dict]]:
    """Cached intraday bars for `symbols` with a timestamp on/after start_day,
    shaped like Alpaca bar dicts (t/o/h/l/c/v/vw) so the backtester consumes
    them unchanged. ISO timestamps sort/compare lexically, so a plain string
    filter on the 'YYYY-MM-DD' prefix is correct."""
    if not symbols:
        return {}
    out: dict[str, list[dict]] = {}
    for i in range(0, len(symbols), 500):
        chunk = symbols[i : i + 500]
        rows = (
            sess.query(IntradayBar)
            .filter(IntradayBar.symbol.in_(chunk), IntradayBar.ts >= start_day)
            .order_by(IntradayBar.symbol, IntradayBar.ts)
            .all()
        )
        for b in rows:
            out.setdefault(b.symbol, []).append(
                {"t": b.ts, "o": b.o, "h": b.h, "l": b.l, "c": b.c, "v": b.v, "vw": b.vw}
            )
    return out


def has_intraday(sess: OrmSession) -> bool:
    """Whether any intraday bars are cached (stage-2 replay is possible)."""
    return sess.query(IntradayBar).first() is not None
