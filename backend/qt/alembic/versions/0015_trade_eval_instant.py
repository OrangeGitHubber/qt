"""Record WHEN the engine looked at the tape, and what it saw.

The engine evaluates every 60 seconds counted from whenever the scheduler last
started, so its looks land at an arbitrary second past the minute — :17, :18 and
:44 have all been observed. A replay evaluates at bar close, on the minute.
Nothing recorded the engine's phase, so the two sample the tape up to a bar apart
and the fidelity report had to quote an irreducible "poll phase floor" (measured
0.045%) for what is not a structural limit at all: it is a fact nobody wrote
down. Written down, the replay can align its clock to the engine's.

`entry_eval_at` / `exit_eval_at` hold the instant the snapshot that decision read
came back. `entry_eval_price` / `exit_eval_price` hold the price it saw then —
which is not entry_price/exit_price (those are fill prices), and on a REJECTED
row is the only price recorded at all.

Null for every existing row, and null for any future row whose price came from
somewhere with no recorded observation instant. That is deliberate: a reader must
be able to tell "the engine looked at 14:01:17" from "we do not know when the
engine looked". Defaulting these to created_at would put a confident wrong number
under the one feature whose entire purpose is measuring accuracy — the same
reasoning as 0014's enabled_at.

Four nullable columns with no server default, so Postgres adds them as metadata
only and months of live history are not rewritten.
"""

import sqlalchemy as sa
from alembic import op

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("trades", sa.Column("entry_eval_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("trades", sa.Column("entry_eval_price", sa.Float(), nullable=True))
    op.add_column("trades", sa.Column("exit_eval_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("trades", sa.Column("exit_eval_price", sa.Float(), nullable=True))


def downgrade() -> None:
    op.drop_column("trades", "exit_eval_price")
    op.drop_column("trades", "exit_eval_at")
    op.drop_column("trades", "entry_eval_price")
    op.drop_column("trades", "entry_eval_at")
