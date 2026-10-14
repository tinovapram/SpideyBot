"""Database plumbing: one async engine and one session factory for the whole process.

Every other module asks this one for a session rather than building its own engine, so
there is a single connection pool and a single place that knows the DSN.

The DSN comes from :func:`app.dbprovider.resolve_dsn`, not from ``Settings.database_url``:
only the provider knows about the password a bundled cluster generated for itself, and
that fallback is what lets a fresh checkout run without a password in ``.env``.

The schema check lives here too, next to the engine, because it needs both the DSN and a
connection — and because "the database is reachable but not at head" is a fact about this
process's database rather than about any one caller.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from typing import Any

from sqlalchemy import text
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app import dbprovider, paths
from app.config import Settings

_engine: AsyncEngine | None = None
_factory: async_sessionmaker[AsyncSession] | None = None


class SchemaOutOfDate(RuntimeError):
    """The database is reachable but not at the migration head."""


def configure(settings: Settings, *, url: str | None = None) -> AsyncEngine:
    """Build the engine once per process.

    ``url`` overrides the configured DSN, which is how the integration suite points at
    whatever Postgres its layered resolver found instead of the configured one.
    """
    global _engine, _factory

    if _engine is not None:
        return _engine

    _engine = create_async_engine(url or dbprovider.resolve_dsn(settings), pool_pre_ping=True)
    # expire_on_commit=False so a caller can still read an object's attributes after the
    # session commits, which matters for handlers that render a message post-commit.
    _factory = async_sessionmaker(_engine, expire_on_commit=False)
    return _engine


def is_configured() -> bool:
    return _factory is not None


@asynccontextmanager
async def session() -> AsyncIterator[AsyncSession]:
    """A session that commits on success and rolls back on failure.

    Committing here rather than in every caller keeps a half-applied quota reservation
    impossible: either the whole unit of work lands or none of it does.
    """
    if _factory is None:
        raise RuntimeError("database not configured: call app.db.configure() at startup")

    async with _factory() as db:
        try:
            yield db
            await db.commit()
        except Exception:
            await db.rollback()
            raise


async def dispose() -> None:
    """Close the pool, so a shutdown does not leave connections behind."""
    global _engine, _factory

    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _factory = None


# ── schema state ────────────────────────────────────────────────────────


def _alembic_config() -> Any:
    """Alembic's configuration with the migration scripts located, and nothing else set.

    Alembic is imported here rather than at module scope so that ``spideybot check`` — which
    must work on a checkout with no database and no ``.env`` — does not pay for it.
    """
    from alembic.config import Config as AlembicConfig

    config = AlembicConfig(str(paths.ALEMBIC_INI))
    config.set_main_option("script_location", str(paths.MIGRATIONS_DIR))
    return config


def alembic_config(settings: Settings) -> Any:
    """The configuration to *migrate* with, pointed at the same database this process uses.

    Built in one place so the CLI, the test fixtures and the start-up check cannot drift on
    which URL or script directory migrations run from.
    """
    config = _alembic_config()
    config.set_main_option("sqlalchemy.url", dbprovider.resolve_dsn(settings))
    return config


def head_revision() -> str | None:
    """The revision the migration scripts end at, or ``None`` when there are none.

    Reads the scripts without touching the database, which is what lets the start-up check
    describe a schema problem even when the DSN cannot be resolved at all.
    """
    with _without_alembic_plugin_banner():
        from alembic.script import ScriptDirectory

        return ScriptDirectory.from_config(_alembic_config()).get_current_head()


@contextmanager
def _without_alembic_plugin_banner() -> Iterator[None]:
    """Silence Alembic's plugin banner for the first import it triggers.

    Importing Alembic's runtime logs a line per plugin it registers. That belongs in the
    output of a migration run by hand, where nothing else is logged; in the bot it arrives
    after logging is configured, so it would print seven lines of noise before every start.
    Only the ``alembic`` logger's level is touched, and only for the length of the import.
    """
    logger = logging.getLogger("alembic")
    previous = logger.level
    logger.setLevel(logging.WARNING)
    try:
        yield
    finally:
        logger.setLevel(previous)


async def applied_revisions() -> set[str]:
    """What the database records, or an empty set when it has never been migrated.

    A set rather than a value because Alembic stores one row per merged branch point.
    """
    if _engine is None:
        raise RuntimeError("database not configured: call app.db.configure() at startup")

    async with _engine.connect() as connection:
        try:
            result = await connection.execute(text("SELECT version_num FROM alembic_version"))
        except ProgrammingError:
            # The table itself is missing, which is the honest answer for a fresh database
            # and not an error worth distinguishing from "nothing applied".
            return set()
        return {row[0] for row in result}


async def require_current_schema() -> None:
    """Raise unless the database is exactly at the migration head.

    An out-of-date schema does not announce itself: the process starts, connects, reports
    itself healthy and then fails on the first query that touches a column the missing
    migration would have added — as a download that vanished for no visible reason. Checking
    once at start-up turns that into a sentence naming the command that fixes it. Migrations
    are deliberately *not* run here: applying DDL is an operator's decision, not a side
    effect of starting a bot.
    """
    expected = head_revision()
    if expected is None:
        return

    applied = await applied_revisions()
    if applied == {expected}:
        return

    recorded = ", ".join(sorted(applied)) if applied else "no migrations applied"
    raise SchemaOutOfDate(
        f"database schema is not at head ({recorded}; expected {expected}). "
        "Run `python main.py db migrate`"
    )
