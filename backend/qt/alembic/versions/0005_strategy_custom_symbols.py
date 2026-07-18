"""Custom-symbols universe: a strategy can target a hand-picked symbol list.

Adds a nullable `symbols` column (JSON array text) to `strategies`. When
universe="custom" the engine trades exactly these symbols — a lightweight
alternative to spinning up a whole basket for a one-off (e.g. an SPCX-only
strategy). Nullable, so SQLite adds it in place without a table rebuild.
"""

import sqlalchemy as sa
from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("strategies", sa.Column("symbols", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("strategies", "symbols")
