"""Shipped strategy TEMPLATES: reference configurations that can only be cloned.

Four styles came out of a long session of measuring why one strategy kept
underperforming: a dip buyer, a trend follower, an intraday scanner rider, and a
DCA baseline. The recurring finding was that each style's settings CONTRADICT the
others' — three separate trend confirmations were measured as near-incompatible
with a dip entry, and stacking them produced a strategy that took four trades in
three months. Writing the four down as starting points is how that lesson stops
having to be re-derived.

A template is deliberately inert: it can never be enabled, edited or deleted, so
it stays a fixed reference rather than drifting into a half-configured live
strategy. Cloning it produces an ordinary strategy the user owns outright.

False for every existing row — nothing already in the database is a template.
"""

import sqlalchemy as sa
from alembic import op

revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "strategies",
        sa.Column("template", sa.Boolean(), nullable=False, server_default=sa.false()),
    )


def downgrade() -> None:
    op.drop_column("strategies", "template")
