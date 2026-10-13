"""Repair installations that recorded the TeraBox migration without the column."""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "b7c1e4a92f31"
down_revision = "8588974059d3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Some existing databases are stamped at 8588974059d3 even though the DDL
    # was not applied.  Keep this repair safe for databases where it already exists.
    op.execute(
        "ALTER TABLE user_prefs ADD COLUMN IF NOT EXISTS terabox_ndus VARCHAR(256)"
    )


def downgrade() -> None:
    op.drop_column("user_prefs", "terabox_ndus")
