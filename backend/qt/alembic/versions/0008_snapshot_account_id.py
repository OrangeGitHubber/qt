"""Tag each benchmark snapshot with the broker account it was measured on.

The scoreboard stores one equity snapshot per day and normalises every point
against the FIRST row ever recorded. That silently breaks across an account
switch: the equity jumps from the old account's balance to the new one's, and
the chart reads the step as a catastrophic loss (a real case showed −80% the day
a paper account was replaced — arithmetic on two unrelated accounts, not a
trading result).

Trades already carry `account_id` (see 0007); this brings snapshots in line, so
the scoreboard can scope to one account and rebase on THAT account's first day.
Nullable — existing rows stay null ("legacy"), viewable via the same
current/all/untagged selector the journal uses.
"""

import sqlalchemy as sa
from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("benchmark_snapshots", sa.Column("account_id", sa.String(length=64), nullable=True))
    op.create_index("ix_benchmark_snapshots_account_id", "benchmark_snapshots", ["account_id"])


def downgrade() -> None:
    op.drop_index("ix_benchmark_snapshots_account_id", table_name="benchmark_snapshots")
    op.drop_column("benchmark_snapshots", "account_id")
