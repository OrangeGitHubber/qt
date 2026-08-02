"""Record where a strategy came from when a parameter search produced it.

The optimizer's honesty rests on one thing: the last ~30% of the window is data
the search never saw, so its score there is a real out-of-sample result. That
guarantee holds exactly ONCE per slice of history.

Optimize a strategy, save the draft, then optimize THAT draft on the same period
and the guarantee is quietly gone — the person at the keyboard is now selecting
configurations with knowledge of how they scored on the "held-out" data, so it
isn't held out any more. After a few rounds that slice has effectively seen
thousands of configurations, while the app keeps printing a confident
out-of-sample number as if it were independent.

Nothing in the data model could see that happening, because each draft was just
another strategy. These three columns give a draft a parent and a window, so the
chain can be walked and the run can say which generation it is.

Null for every existing strategy, which is correct: a strategy nobody optimized
has no ancestry, and generation 1 is the honest reading.
"""

import sqlalchemy as sa
from alembic import op

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "strategies",
        sa.Column("optimized_from_id", sa.Integer(), nullable=True),
    )
    op.add_column("strategies", sa.Column("optimized_days", sa.Integer(), nullable=True))
    op.add_column(
        "strategies",
        sa.Column("optimized_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("strategies", "optimized_at")
    op.drop_column("strategies", "optimized_days")
    op.drop_column("strategies", "optimized_from_id")
