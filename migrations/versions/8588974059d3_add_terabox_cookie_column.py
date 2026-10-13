"""Add TeraBox cookie column."""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "8588974059d3"
down_revision = "f066b223bff1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("user_prefs", sa.Column("terabox_ndus", sa.String(length=256), nullable=True))


def downgrade() -> None:
    op.drop_column("user_prefs", "terabox_ndus")
