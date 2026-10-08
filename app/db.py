"""Database plumbing: one async engine and one session factory for the whole process.

Every other module asks this one for a session rather than building its own engine, so
there is a single connection pool and a single place that knows the DSN.

The DSN comes from :func:`app.dbprovider.resolve_dsn`, not from ``Settings.database_url``:
only the provider knows about the password a bundled cluster generated for itself, and
that fallback is what lets a fresh checkout run without a password in ``.env``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app import dbprovider
from app.config import Settings

_engine: AsyncEngine | None = None
_factory: async_sessionmaker[AsyncSession] | None = None


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
