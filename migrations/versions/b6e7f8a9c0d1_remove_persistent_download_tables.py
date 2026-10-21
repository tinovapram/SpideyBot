"""remove obsolete persistent download tables

Revision ID: b6e7f8a9c0d1
Revises: a1b2c3d4e5f6
Create Date: 2026-10-21

The download queue is now process-local, and download history is no longer a
feature. User, quota, referral, cookie, and Telegram session data are kept.
"""
from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "b6e7f8a9c0d1"
down_revision: str | None = "a1b2c3d4e5f6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # download_history references download_jobs, so it must be removed first.
    op.drop_table("download_history")
    op.drop_table("download_jobs")


def downgrade() -> None:
    raise NotImplementedError(
        "The removed persistent download schema cannot be restored automatically."
    )
