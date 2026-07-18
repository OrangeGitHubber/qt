"""Phase 3.5: themed baskets + top-N ranking universe.

Baskets are curated symbol groups (Defense, Banking, …) stored as a parent
`baskets` row plus a `basket_items` join table of (symbol, asset_class) pairs
— queryable and dedup-by-primary-key, rather than a JSON blob. Strategies
gain a "basket" universe: pick a basket, a ranking metric and a top_n.
"""

import sqlalchemy as sa
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "baskets",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("name", sa.String(80), nullable=False),
        sa.Column("builtin", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "basket_items",
        sa.Column("basket_id", sa.Integer(), sa.ForeignKey("baskets.id"), primary_key=True),
        sa.Column("symbol", sa.String(32), primary_key=True),
        sa.Column("asset_class", sa.String(16), primary_key=True),
    )
    # New strategy universe: "basket". Add plain columns (nullable / with a
    # server default) so SQLite doesn't have to rebuild the table. basket_id is
    # a soft reference — the FK is declared on the ORM model for query building;
    # enforcing it at the DB level would force a batch table-rebuild here for no
    # real benefit on a single-writer SQLite file.
    op.add_column("strategies", sa.Column("basket_id", sa.Integer(), nullable=True))
    op.add_column(
        "strategies",
        sa.Column("rank_by", sa.String(24), nullable=False, server_default="momentum_today"),
    )
    op.add_column(
        "strategies", sa.Column("top_n", sa.Integer(), nullable=False, server_default="10")
    )


def downgrade() -> None:
    op.drop_column("strategies", "top_n")
    op.drop_column("strategies", "rank_by")
    op.drop_column("strategies", "basket_id")
    op.drop_table("basket_items")
    op.drop_table("baskets")
