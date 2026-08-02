"""Record WHEN a strategy was switched on.

Enabling is the moment the engine was first allowed to act, so it is the honest
start of any comparison between what the strategy really did and what a replay
of it would have done. Nothing recorded it: enabling flips a boolean and writes
an audit line, and a boolean has no timestamp. The fidelity report therefore had
no way to say "activated at 14:02" — or to start its window there for a strategy
that has been live for an hour and hasn't traded yet.

Null for every existing strategy, including ones that are already enabled. That
is honest rather than convenient: we genuinely do not know when those were
switched on, and inventing a timestamp would put a confident wrong number under
a feature whose entire purpose is measuring accuracy. The report falls back to
the audit log for those, and says where the answer came from.
"""

import sqlalchemy as sa
from alembic import op

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "strategies",
        sa.Column("enabled_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("strategies", "enabled_at")
