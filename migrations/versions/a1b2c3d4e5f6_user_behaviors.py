"""add timer_media_enabled and magic_word to users

Revision ID: a1b2c3d4e5f6
Revises: 7c1f2a3b4d5e
Create Date: 2026-09-10

"""
from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = 'a1b2c3d4e5f6'
down_revision: str | None = '7c1f2a3b4d5e'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

def upgrade() -> None:
    op.add_column('users', sa.Column('timer_media_enabled', sa.Boolean(), nullable=False, server_default=sa.text('true')))
    op.add_column('users', sa.Column('magic_word', sa.String(length=50), nullable=False, server_default='WOW'))

def downgrade() -> None:
    op.drop_column('users', 'magic_word')
    op.drop_column('users', 'timer_media_enabled')
