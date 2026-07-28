"""Tag each trade with the broker account it was made on.

After liquidating and switching to a different Alpaca account, the journal and
per-strategy P&L views were still showing the OLD account's trades. Stamping each
trade with the account (Alpaca account number) lets those views filter by account
and default to the current one. Nullable — existing trades stay null ("legacy")
until a one-time backfill assigns them the old account id.
"""

import sqlalchemy as sa
from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("trades", sa.Column("account_id", sa.String(length=64), nullable=True))
    op.create_index("ix_trades_account_id", "trades", ["account_id"])


def downgrade() -> None:
    op.drop_index("ix_trades_account_id", table_name="trades")
    op.drop_column("trades", "account_id")
