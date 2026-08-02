"""Snapshot a basket's members whenever they change.

A strategy's config version records WHICH basket it uses, never WHO is in it. So
adding or removing a symbol changes what that strategy trades while its own
config version stays byte-identical — and anything reconstructing history later
reads today's members and concludes nothing moved.

That is worse than having no record at all. The backtest-fidelity check compares
the config that produced a trade against the config being replayed; without this
it would replay today's basket against trades made from a different list and
report "no configuration drift", which is a confident statement of something
false.

No backfill is possible — the old membership was never written down. Existing
baskets get their first snapshot the next time they are edited, and the fidelity
report says "unknown" rather than guessing for anything older.
"""

import sqlalchemy as sa
from alembic import op

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "basket_versions",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("basket_id", sa.Integer(), sa.ForeignKey("baskets.id"), nullable=False, index=True),
        sa.Column("version_no", sa.Integer(), nullable=False),
        sa.Column("snapshot", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, index=True),
    )


def downgrade() -> None:
    op.drop_table("basket_versions")
