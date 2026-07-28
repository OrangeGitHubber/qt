"""Opt-in ranking flag: rank a universe's pool and keep the top N.

Top-N ranking (by momentum / 30-day return / relative strength / RS-vs-SPY) used
to be wired to basket universes only. This adds a `rank_enabled` boolean so a
**watchlist** or **custom** strategy can opt into the same ranking — e.g. "trade
the top 5 of my 20-symbol watchlist by relative strength."

Default is FALSE (consider the whole pool, entry rules decide) so existing
watchlist/custom strategies are unchanged. Existing **basket** strategies are set
to TRUE, because a basket has always been ranked — preserving their behaviour.
Nullable-with-default so SQLite adds it in place.
"""

import sqlalchemy as sa
from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "strategies",
        sa.Column("rank_enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    # Baskets have always ranked; keep them ranking after this migration.
    op.execute("UPDATE strategies SET rank_enabled = 1 WHERE universe = 'basket'")


def downgrade() -> None:
    op.drop_column("strategies", "rank_enabled")
