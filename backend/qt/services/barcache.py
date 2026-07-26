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
        change = (q.close / q.prev_close - 1) * 100
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
