"""Baseline: Phase 0/1 schema (settings, secrets, audit_log, watchlist).

Existing databases created before Alembic are stamped with this revision
instead of re-running it (see qt.db.init_db).
"""

import sqlalchemy as sa
from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "settings",
        sa.Column("key", sa.String(128), primary_key=True),
        sa.Column("value", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "secrets",
        sa.Column("name", sa.String(128), primary_key=True),
        sa.Column("ciphertext", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "audit_log",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("at", sa.DateTime(timezone=True), nullable=False, index=True),
        sa.Column("category", sa.String(64), nullable=False, index=True),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("detail", sa.Text(), nullable=False),
    )
    op.create_table(
        "watchlist",
        sa.Column("symbol", sa.String(32), primary_key=True),
        sa.Column("asset_class", sa.String(16), primary_key=True),
        sa.Column("added_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    for table in ("watchlist", "audit_log", "secrets", "settings"):
        op.drop_table(table)
