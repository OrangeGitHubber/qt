"""A freeform notes field on each strategy.

Somewhere to keep your own reasoning: what you were testing, what a backtest
suggested, what to try next. Purely for the human — the engine never reads it.

Deliberately NOT part of the config-version snapshot's meaning: editing a note
changes no trading behaviour, so it must not mint a new config version (see
qt/api/strategies.py). Every trade points at the config that produced it, and a
version bump that changed nothing would make that history lie.
"""

import sqlalchemy as sa
from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("strategies", sa.Column("notes", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("strategies", "notes")
