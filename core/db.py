"""Async database engine and session management (PostgreSQL)."""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import AsyncIterator

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from core import config, models

_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def init_db() -> None:
    """Create the async engine and session factory. Call once at startup."""
    global _engine, _session_factory
    settings = config.get_settings()
    _engine = create_async_engine(settings.database_url, echo=False, pool_pre_ping=True)
    _session_factory = async_sessionmaker(_engine, expire_on_commit=False)


def engine() -> AsyncEngine:
    """Return the async engine, raising if not initialized."""
    if _engine is None:
        raise RuntimeError("init_db() must be called before engine()")
    return _engine


async def create_all() -> None:
    """Create all tables (dev convenience; Alembic is the source of truth)."""
    async with engine().begin() as conn:
        await conn.run_sync(models.Base.metadata.create_all)


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    """Yield a session within a transaction; commits on success, rolls back on error."""
    if _session_factory is None:
        raise RuntimeError("init_db() must be called before session_scope()")
    async with _session_factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def dispose() -> None:
    """Close the engine's connection pool (shutdown)."""
    if _engine is not None:
        await _engine.dispose()
