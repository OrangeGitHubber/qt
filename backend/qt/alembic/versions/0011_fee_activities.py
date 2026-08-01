"""Record the fees Alpaca actually charged.

QT recorded no real fees at all: every per-trade P&L in the journal was gross.
That understates crypto results by roughly half a percent per round trip
(0.15-0.25% per side), which is a large share of the edge a momentum strategy
is chasing.

WHY A TABLE AND NOT JUST A COLUMN ON trades
-------------------------------------------
Alpaca posts crypto fees as CFEE account activities, end of day, and a CFEE is
a *non-trade* activity. Its documented fields are:

    id, activity_type, date, net_amount, description, symbol, qty, price, status

There is no order_id, no side, and no timestamp — only `date`. So for a day on
which two strategies both traded ETH, or one strategy round-tripped it three
times, nothing in the payload says which fill a given fee belongs to. Matching
on symbol + day + side would be inference dressed up as bookkeeping, and this
project's rule is that a guess is never presented as a fact.

So the fees are stored at the granularity Alpaca actually reports them: one row
per activity, keyed by Alpaca's own activity id. That is a fact we can defend —
"the account paid $X in fees on this day for this symbol" — and it loses
nothing, because the raw payload is kept alongside.

trades.fees is added anyway, nullable and deliberately left NULL. It is the
landing place for a future attribution that is actually reliable (Alpaca adding
an order id to CFEE, or a broker that reports fees per fill). Null renders as
"—" in the UI; it must never render as $0.00, because zero reads as "no fee was
charged" and that would be a lie.

IDEMPOTENCY. The primary key is Alpaca's activity id (a string), not an
autoincrement. The sync job re-scans an overlapping window every run — it has
to, because fees post late — so without a natural key a second run would double
every fee. Double-counted fees are worse than missing ones: they look
plausible.
"""

import sqlalchemy as sa
from alembic import op

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("trades", sa.Column("fees", sa.Float(), nullable=True))
    op.create_table(
        "fee_activities",
        sa.Column("id", sa.String(128), primary_key=True),
        sa.Column("account_id", sa.String(64), nullable=True, index=True),
        sa.Column("activity_type", sa.String(16), nullable=False, index=True),
        sa.Column("day", sa.String(10), nullable=False, index=True),
        sa.Column("symbol", sa.String(32), nullable=True, index=True),
        sa.Column("qty", sa.Float(), nullable=True),
        sa.Column("price", sa.Float(), nullable=True),
        sa.Column("usd_amount", sa.Float(), nullable=True),
        sa.Column("usd_is_estimate", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column("raw", sa.Text(), nullable=False, server_default=""),
        sa.Column("synced_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_table("fee_activities")
    op.drop_column("trades", "fees")
