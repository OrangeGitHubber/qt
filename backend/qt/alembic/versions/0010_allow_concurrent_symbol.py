"""Let a strategy hold a symbol another strategy already holds.

The "position already open for this symbol" rail is ACCOUNT-WIDE: whoever gets
there first owns the name, and every other strategy is blocked out of it. That's
the safe default and stays the default — but two strategies with different theses
on the same stock is a legitimate book, and the rail made it impossible.

Off for every existing strategy: this changes nothing until you turn it on.

NOT loosened by this flag, deliberately: the wash-sale guard and the
cooldown-after-a-loss stay portfolio-wide. Those exist to protect the ACCOUNT
(the IRS counts the account, not your strategies), so per-strategy exemptions
would defeat the point.
"""

import sqlalchemy as sa
from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "strategies",
        sa.Column("allow_concurrent_symbol", sa.Boolean(), nullable=False, server_default=sa.false()),
    )


def downgrade() -> None:
    op.drop_column("strategies", "allow_concurrent_symbol")
