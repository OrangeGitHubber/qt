"""Local mirror of Alpaca's asset list for symbol autocomplete."""

import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "assets",
        sa.Column("symbol", sa.String(32), primary_key=True),
        sa.Column("asset_class", sa.String(16), primary_key=True),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("exchange", sa.String(32), nullable=False),
        sa.Column("fractionable", sa.Boolean(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_assets_name", "assets", ["name"])


def downgrade() -> None:
    op.drop_index("ix_assets_name", table_name="assets")
    op.drop_table("assets")
