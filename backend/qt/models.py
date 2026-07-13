from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class Setting(Base):
    """A single UI-editable configuration value, stored as JSON text."""

    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    value: Mapped[str] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class Secret(Base):
    """A sensitive value (API keys), encrypted at rest with the instance key."""

    __tablename__ = "secrets"

    name: Mapped[str] = mapped_column(String(128), primary_key=True)
    ciphertext: Mapped[str] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class WatchlistItem(Base):
    """A symbol the user pinned for the scanner/engine to always consider."""

    __tablename__ = "watchlist"

    symbol: Mapped[str] = mapped_column(String(32), primary_key=True)
    asset_class: Mapped[str] = mapped_column(String(16), primary_key=True)  # stock | crypto
    added_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AuditLog(Base):
    """Append-only record of every consequential action the app takes."""

    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    category: Mapped[str] = mapped_column(String(64), index=True)
    message: Mapped[str] = mapped_column(Text)
    detail: Mapped[str] = mapped_column(Text, default="")


class Strategy(Base):
    """A UI-configured trading strategy. `params` holds the entry/exit rules
    as JSON; its shape is validated by the API layer (StrategyParams)."""

    __tablename__ = "strategies"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(80))
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    asset_class: Mapped[str] = mapped_column(String(16))  # stock | crypto
    universe: Mapped[str] = mapped_column(String(16), default="scanner")  # scanner | watchlist | both
    preset: Mapped[str] = mapped_column(String(48), default="custom")
    params: Mapped[str] = mapped_column(Text)  # JSON: entry/exit rules
    sizing_usd: Mapped[float] = mapped_column(Float, default=200.0)  # $ per trade
    sleeve_usd: Mapped[float] = mapped_column(Float, default=1000.0)  # max $ this strategy may hold
    max_positions: Mapped[int] = mapped_column(Integer, default=3)
    swing_mode: Mapped[bool] = mapped_column(Boolean, default=True)  # no same-day exits except stops
    ignore_regime: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class StrategyConfigVersion(Base):
    """Immutable snapshot of a strategy's full config; every trade points at
    the version that produced it so stats stay honest across edits."""

    __tablename__ = "strategy_config_versions"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    strategy_id: Mapped[int] = mapped_column(ForeignKey("strategies.id"), index=True)
    version_no: Mapped[int] = mapped_column(Integer)
    snapshot: Mapped[str] = mapped_column(Text)  # full JSON of the strategy at save time
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Trade(Base):
    """One position lifecycle (entry → exit), in any mode including shadow."""

    __tablename__ = "trades"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    strategy_id: Mapped[int] = mapped_column(ForeignKey("strategies.id"), index=True)
    config_version_id: Mapped[int | None] = mapped_column(
        ForeignKey("strategy_config_versions.id"), nullable=True
    )
    mode: Mapped[str] = mapped_column(String(16), index=True)  # shadow | paper | live
    symbol: Mapped[str] = mapped_column(String(32), index=True)
    asset_class: Mapped[str] = mapped_column(String(16))
    side: Mapped[str] = mapped_column(String(8), default="long")
    qty: Mapped[float] = mapped_column(Float, default=0.0)
    notional: Mapped[float] = mapped_column(Float, default=0.0)  # $ at entry
    status: Mapped[str] = mapped_column(String(16), index=True)  # open | closed | rejected
    entry_reason: Mapped[str] = mapped_column(Text, default="")
    exit_reason: Mapped[str] = mapped_column(Text, default="")
    entry_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    entry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    entry_order_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    exit_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    exit_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    exit_order_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    high_water: Mapped[float | None] = mapped_column(Float, nullable=True)  # trailing-stop anchor
    pnl: Mapped[float | None] = mapped_column(Float, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)


class BenchmarkSnapshot(Base):
    """Daily record for the scoreboard: bot equity vs buy-and-hold anchors."""

    __tablename__ = "benchmark_snapshots"

    day: Mapped[str] = mapped_column(String(10), primary_key=True)  # YYYY-MM-DD (UTC)
    bot_equity: Mapped[float] = mapped_column(Float)
    spy_close: Mapped[float | None] = mapped_column(Float, nullable=True)
    btc_close: Mapped[float | None] = mapped_column(Float, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
