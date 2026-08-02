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
    universe: Mapped[str] = mapped_column(String(16), default="scanner")  # scanner | watchlist | both | basket | custom
    # When it was last switched ON. The moment the engine was allowed to act, so
    # it is where a fidelity comparison starts for a strategy that hasn't traded
    # yet. Null on strategies enabled before this was recorded — see 0014.
    enabled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    basket_id: Mapped[int | None] = mapped_column(ForeignKey("baskets.id"), nullable=True)
    symbols: Mapped[str | None] = mapped_column(Text, nullable=True)  # JSON list for universe="custom"
    rank_by: Mapped[str] = mapped_column(String(24), default="momentum_today")  # momentum_today | return_30d | relative_strength | rs_vs_spy
    top_n: Mapped[int] = mapped_column(Integer, default=10)  # take the top N ranked symbols
    # When true, rank the universe's pool by rank_by and keep the top N (basket
    # is always ranked; watchlist/custom opt in). False = consider the whole pool.
    rank_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    preset: Mapped[str] = mapped_column(String(48), default="custom")
    params: Mapped[str] = mapped_column(Text)  # JSON: entry/exit rules
    sizing_usd: Mapped[float] = mapped_column(Float, default=200.0)  # $ per trade
    sleeve_usd: Mapped[float] = mapped_column(Float, default=1000.0)  # max $ this strategy may hold
    max_positions: Mapped[int] = mapped_column(Integer, default=3)
    swing_mode: Mapped[bool] = mapped_column(Boolean, default=True)  # no same-day exits except stops
    ignore_regime: Mapped[bool] = mapped_column(Boolean, default=False)
    # Allow this strategy to open a position in a symbol ANOTHER strategy already
    # holds. Off by default — the account-wide "one position per symbol" rail is
    # the safe behaviour. It never lets a strategy stack a second position on its
    # OWN holding (that's scaling-in, a separate feature), and it does not touch
    # the wash-sale guard or the loss cooldown, which stay portfolio-wide.
    allow_concurrent_symbol: Mapped[bool] = mapped_column(Boolean, default=False)
    # Your own notes: what you were testing, what a backtest suggested, what to
    # try next. The engine never reads this — it exists purely so the reasoning
    # behind a strategy lives next to the strategy instead of in your head.
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Set only when a parameter search produced this strategy: which strategy it
    # was searched FROM, and over how many days. The optimizer walks this chain
    # to tell you which generation a run is — its out-of-sample guarantee is a
    # once-per-slice resource, and re-optimizing a draft on the same history
    # spends it without anything visibly changing.
    optimized_from_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    optimized_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    optimized_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
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
    # The broker account this trade was made on (Alpaca account number). Lets the
    # journal / P&L views separate accounts after a key switch. Null = made before
    # tagging existed (legacy) — a one-time SQL backfill can stamp those.
    account_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
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
    # Broker fees charged for THIS trade, in USD. Null means "not known per
    # trade" and must render as "—", never $0.00 — zero would claim the broker
    # charged nothing, which we cannot know.
    #
    # In practice this stays null today: Alpaca posts crypto fees as CFEE
    # activities that carry no order id and only a DATE, so a fee cannot be tied
    # to one fill (see qt/services/fees.py). The real figures live in
    # FeeActivity at day/account level. The column exists so that if Alpaca ever
    # attaches an order id — or we move to a broker that does — attribution has
    # somewhere honest to land without another migration.
    fees: Mapped[float | None] = mapped_column(Float, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)


class FeeActivity(Base):
    """One fee Alpaca actually charged, exactly as the broker reported it.

    Alpaca charges 0.15-0.25% per side on crypto (US equities are commission-
    free) and posts each one as an account activity of type CFEE (or FEE) at
    END OF DAY — never at fill time. See qt/services/fees.py for the full
    response shape and why these rows are the account's fee truth while
    Trade.fees stays null.

    The primary key is ALPACA'S OWN activity id, not an autoincrement. That is
    the whole idempotency guarantee: re-syncing an overlapping date window (the
    job deliberately does, because fees post late) re-sees the same ids and
    inserts nothing. A surrogate key would have let a second run double every
    fee, which is exactly the failure that makes a fee report worse than no
    report at all.
    """

    __tablename__ = "fee_activities"

    # e.g. "20220812000000000::53be51ba-46f9-43de-b81f-576f241dc680"
    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    # Which broker account was charged. Nullable because the activity payload
    # itself doesn't always carry one — we stamp the account we fetched it with.
    account_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    activity_type: Mapped[str] = mapped_column(String(16), index=True)  # CFEE | FEE
    day: Mapped[str] = mapped_column(String(10), index=True)  # YYYY-MM-DD, the activity's `date`
    # Slash-less as Alpaca sends it ('ETHUSD'); qt stores trades as 'ETH/USD'.
    # Nullable: a plain FEE (e.g. a regulatory fee) may name no symbol.
    symbol: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    # The fee in the asset it was CHARGED IN, which for a crypto buy is the coin
    # you received, not dollars. Alpaca sends it negative (a debit).
    qty: Mapped[float | None] = mapped_column(Float, nullable=True)
    price: Mapped[float | None] = mapped_column(Float, nullable=True)  # broker's mark for that asset
    # The fee in USD, or null when we genuinely cannot tell. Paired with
    # usd_is_estimate rather than silently blending the two: `net_amount` is the
    # broker's own dollar figure (a fact), while a coin-denominated fee only
    # becomes dollars by multiplying by the broker's mark (an estimate). The UI
    # has to be able to say which one it is showing.
    usd_amount: Mapped[float | None] = mapped_column(Float, nullable=True)
    usd_is_estimate: Mapped[bool] = mapped_column(Boolean, default=False)
    description: Mapped[str] = mapped_column(Text, default="")
    # The untouched payload. Alpaca's non-trade activity shape is thinly
    # documented and may gain fields (an order id would be the big one); keeping
    # the original means a better attribution can be derived later from rows we
    # already have, instead of re-fetching history we may no longer be able to.
    raw: Mapped[str] = mapped_column(Text, default="")
    synced_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Asset(Base):
    """Local mirror of Alpaca's tradable asset list, refreshed daily.

    Reference data, not user data: rebuildable from the API at any time, so
    it's safe to wipe. Exists so symbol autocomplete searches locally and
    never spends an API request per keystroke.
    """

    __tablename__ = "assets"

    symbol: Mapped[str] = mapped_column(String(32), primary_key=True)
    asset_class: Mapped[str] = mapped_column(String(16), primary_key=True)  # stock | crypto
    name: Mapped[str] = mapped_column(String(255), default="")
    exchange: Mapped[str] = mapped_column(String(32), default="")
    fractionable: Mapped[bool] = mapped_column(Boolean, default=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class BasketVersion(Base):
    """Immutable snapshot of a basket's MEMBERS whenever they change.

    A strategy's config version records which basket it points at — not who was
    in it. So editing the basket silently changes what that strategy trades
    while its own config version stays byte-identical, and any later attempt to
    reconstruct history reads today's members and believes nothing moved. That
    is worse than having no record: the backtest-fidelity check would compare a
    replay of today's members against trades made from a different list and
    report "no configuration drift", actively reassuring you.

    Snapshots are keyed only by time — the version live at a given moment is the
    newest one created at or before it — so nothing else has to carry a version
    id for the history to be recoverable."""

    __tablename__ = "basket_versions"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    basket_id: Mapped[int] = mapped_column(ForeignKey("baskets.id"), index=True)
    version_no: Mapped[int] = mapped_column(Integer)
    snapshot: Mapped[str] = mapped_column(Text)  # JSON: [{symbol, asset_class}, ...]
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)


class Basket(Base):
    """A named group of symbols — a curated theme/sector list (e.g. Defense,
    Banking). NOT an authoritative sector database: the starter set is
    hand-picked and drifts as companies change; the user edits freely."""

    __tablename__ = "baskets"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(80))
    builtin: Mapped[bool] = mapped_column(Boolean, default=False)  # shipped starter; still editable
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class BasketItem(Base):
    """One (symbol, asset_class) membership in a basket.

    A join table rather than a JSON blob on Basket: members are queryable
    (join against the Asset directory to validate/annotate them), dedup is a
    primary-key guarantee, and add/remove is a single-row op with no
    read-modify-write race."""

    __tablename__ = "basket_items"

    basket_id: Mapped[int] = mapped_column(ForeignKey("baskets.id"), primary_key=True)
    symbol: Mapped[str] = mapped_column(String(32), primary_key=True)
    asset_class: Mapped[str] = mapped_column(String(16), primary_key=True)  # stock | crypto


class BenchmarkSnapshot(Base):
    """Daily record for the scoreboard: bot equity vs buy-and-hold anchors."""

    __tablename__ = "benchmark_snapshots"

    day: Mapped[str] = mapped_column(String(10), primary_key=True)  # YYYY-MM-DD (UTC)
    bot_equity: Mapped[float] = mapped_column(Float)
    spy_close: Mapped[float | None] = mapped_column(Float, nullable=True)
    btc_close: Mapped[float | None] = mapped_column(Float, nullable=True)
    # The broker account this equity was measured on. Without it the scoreboard
    # normalised every point against the first row EVER recorded, so switching
    # accounts made the equity step read as a catastrophic loss. Nullable:
    # pre-0008 rows are "legacy" (see the migration).
    account_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    # The broker account this equity was measured on. Without it the scoreboard
    # normalised every point against the first row EVER recorded, so switching
    # accounts made the equity step read as a catastrophic loss. Nullable:
    # pre-0008 rows are "legacy" (see the migration).
    account_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
