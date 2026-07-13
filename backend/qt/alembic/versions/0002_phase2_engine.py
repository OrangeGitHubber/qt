"""Phase 2: strategies, config versions, trades, benchmark snapshots."""

import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "strategies",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("name", sa.String(80), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("asset_class", sa.String(16), nullable=False),
        sa.Column("universe", sa.String(16), nullable=False),
        sa.Column("preset", sa.String(48), nullable=False),
        sa.Column("params", sa.Text(), nullable=False),
        sa.Column("sizing_usd", sa.Float(), nullable=False),
        sa.Column("sleeve_usd", sa.Float(), nullable=False),
        sa.Column("max_positions", sa.Integer(), nullable=False),
        sa.Column("swing_mode", sa.Boolean(), nullable=False),
        sa.Column("ignore_regime", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "strategy_config_versions",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("strategy_id", sa.Integer(), sa.ForeignKey("strategies.id"), nullable=False, index=True),
        sa.Column("version_no", sa.Integer(), nullable=False),
        sa.Column("snapshot", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "trades",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("strategy_id", sa.Integer(), sa.ForeignKey("strategies.id"), nullable=False, index=True),
        sa.Column("config_version_id", sa.Integer(), sa.ForeignKey("strategy_config_versions.id"), nullable=True),
        sa.Column("mode", sa.String(16), nullable=False, index=True),
        sa.Column("symbol", sa.String(32), nullable=False, index=True),
        sa.Column("asset_class", sa.String(16), nullable=False),
        sa.Column("side", sa.String(8), nullable=False),
        sa.Column("qty", sa.Float(), nullable=False),
        sa.Column("notional", sa.Float(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, index=True),
        sa.Column("entry_reason", sa.Text(), nullable=False),
        sa.Column("exit_reason", sa.Text(), nullable=False),
        sa.Column("entry_price", sa.Float(), nullable=True),
        sa.Column("entry_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("entry_order_id", sa.String(64), nullable=True),
        sa.Column("exit_price", sa.Float(), nullable=True),
        sa.Column("exit_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("exit_order_id", sa.String(64), nullable=True),
        sa.Column("high_water", sa.Float(), nullable=True),
        sa.Column("pnl", sa.Float(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, index=True),
    )
    op.create_table(
        "benchmark_snapshots",
        sa.Column("day", sa.String(10), primary_key=True),
        sa.Column("bot_equity", sa.Float(), nullable=False),
        sa.Column("spy_close", sa.Float(), nullable=True),
        sa.Column("btc_close", sa.Float(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    for table in ("benchmark_snapshots", "trades", "strategy_config_versions", "strategies"):
        op.drop_table(table)
