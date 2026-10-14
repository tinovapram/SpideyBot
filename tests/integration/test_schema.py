"""The start-up schema check, against the database this suite migrates.

The check is the one start-up step that must never produce a false positive: if it disagreed
with Alembic about what "at head" means, a healthy deployment would refuse to start. That claim
is about the real ``alembic_version`` table, so it is asserted here rather than only against
stubbed revisions.
"""

from __future__ import annotations

import pytest

from app import db


async def test_a_migrated_database_is_accepted(engine) -> None:
    """The suite's own database is migrated to head, so the check has to pass on it."""
    await db.require_current_schema()


async def test_the_recorded_revision_is_read_from_the_database(engine, monkeypatch) -> None:
    """A head the database does not have is reported along with what it does have."""
    real_head = db.head_revision()
    assert real_head is not None, "the migrations directory must define a head"
    monkeypatch.setattr(db, "head_revision", lambda: "revision_that_does_not_exist")

    with pytest.raises(db.SchemaOutOfDate) as raised:
        await db.require_current_schema()

    assert real_head in str(raised.value)
    assert "db migrate" in str(raised.value)
