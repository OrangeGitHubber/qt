"""Mode becomes a PER-STRATEGY attribute, so shadow, paper and live coexist.

Until now the engine ran in exactly one mode: a single `engine_mode` setting,
read once per tick and applied to everything enabled. That makes "go live" an
all-or-nothing switch over every strategy at once — including the ones still
being tested. This account currently has 18 enabled strategies, several of them
named "tester".

Werner's requirement, stated 2026-08-04:

    "no no, not everyone will be running two containers. I will because I'm
     doing testing and dev. by default you should be able to run paper next to
     live next to shadow"

So the product must run all three side by side in ONE instance, and a separate
live container is his dev convenience rather than the design.

BACKFILLED FROM THE CURRENT GLOBAL MODE, not from the default. Every existing
row takes whatever `engine_mode` says right now (here: 'paper'), so upgrading
changes the behaviour of nothing. Defaulting them to 'shadow' would have
silently stopped 18 working strategies from placing orders, and defaulting them
to 'paper' would be wrong on an instance whose global mode was 'shadow' — it has
to be read, not assumed.

New strategies default to 'shadow': the mode that touches no broker at all. A
strategy has to be deliberately moved up, never down into safety by accident.

`engine_mode` SURVIVES as a master switch and a CEILING — see
engine.effective_mode. It is the one place to stop everything.
"""

import json

import sqlalchemy as sa
from alembic import op

revision = "0017"
down_revision = "0016"
branch_labels = None
depends_on = None

# Anything not in here — including a missing row, an 'off', or a value from some
# later vocabulary — backfills to 'shadow', the mode that cannot place an order.
# 'off' is a master-switch state rather than a strategy mode: a strategy that was
# not running has no established behaviour to preserve, so it gets the safe one.
_GLOBAL_TO_STRATEGY = {"shadow": "shadow", "paper": "paper", "live": "live"}


def _current_global_mode(conn) -> str:
    """Read `engine_mode` out of the settings table.

    Settings are stored as JSON, so 'paper' is on disk as '"paper"'. Falls back
    to 'shadow' on anything unreadable — a backfill that cannot determine the
    old behaviour must choose the mode that spends no money."""
    try:
        row = conn.execute(
            sa.text("SELECT value FROM settings WHERE key = 'engine_mode'")
        ).fetchone()
    except Exception:  # noqa: BLE001 — a fresh database has no settings row yet
        return "shadow"
    if not row or row[0] is None:
        return "shadow"
    raw = row[0]
    try:
        raw = json.loads(raw)
    except (TypeError, ValueError):
        pass
    return _GLOBAL_TO_STRATEGY.get(str(raw).strip().lower(), "shadow")


def upgrade() -> None:
    op.add_column(
        "strategies",
        sa.Column("mode", sa.String(16), nullable=False, server_default="shadow"),
    )
    conn = op.get_bind()
    inherited = _current_global_mode(conn)
    # TEMPLATES stay 'shadow' whatever the global mode was. They can never be
    # enabled, so they never trade — but a template carrying 'live' would be
    # cloned into a live strategy by one click, which is precisely the accident
    # the promotion path exists to prevent.
    conn.execute(
        sa.text("UPDATE strategies SET mode = :m WHERE template = 0"),
        {"m": inherited},
    )
    op.create_index("ix_strategies_mode", "strategies", ["mode"])


def downgrade() -> None:
    op.drop_index("ix_strategies_mode", table_name="strategies")
    op.drop_column("strategies", "mode")
