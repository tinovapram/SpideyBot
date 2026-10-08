"""Integration fixtures: a real Postgres, found by the plan's layered resolver.

Confirmed against a real server rather than a stand-in, because what is under test here —
``INSERT ... ON CONFLICT ... WHERE ... RETURNING``, and later ``FOR UPDATE SKIP LOCKED``
and lease expiry — *is* Postgres semantics. A substitute would only test the substitute.

Only a database is required: the Telegram credentials are synthesised, so this suite runs
on a checkout whose `.env` has never been filled in.
"""

from __future__ import annotations

import os

import pytest
import pytest_asyncio
from sqlalchemy import text

from app import config, db, dbprovider, paths
from app.config import Settings
from app.models import Base

FAKE_TELEGRAM = {
    "tg_api_id": "1",
    "tg_api_hash": "a" * 32,
    "tg_bot_token": "1:fake",
}


@pytest.fixture(scope="session")
def settings() -> Settings:
    """Settings that need only a database.

    ``load_env_file`` is called inside a snapshot of the environment: it is what picks up
    a local password, but it writes into ``os.environ`` for the whole process, which would
    hand the *unit* suite a developer's real credentials and make tests that assert on
    absent secrets fail. Snapshotting keeps the benefit without the leak.
    """
    snapshot = dict(os.environ)
    try:
        config.load_env_file()
        return config.build_settings(**FAKE_TELEGRAM)
    finally:
        os.environ.clear()
        os.environ.update(snapshot)


def _as_asyncpg(value: str) -> str:
    """Accept a plain ``postgresql://`` URL and make it one asyncpg can use."""
    if "+asyncpg" in value:
        return value
    return value.replace("postgresql://", "postgresql+asyncpg://", 1)


@pytest.fixture(scope="session")
def dsn(settings: Settings) -> str:
    """The layered resolver: an explicit test DSN first, then the bundled cluster.

    Skips rather than fails when neither exists, so a checkout with no Postgres still has
    a green unit suite instead of a wall of connection errors.
    """
    if settings.test_database_url is not None:
        return _as_asyncpg(settings.test_database_url.get_secret_value())

    if dbprovider.is_initialised():
        if not dbprovider.is_running():
            dbprovider.start(settings)
        return dbprovider.resolve_dsn(settings)

    pytest.skip(
        "no Postgres reachable: run `python main.py db init` then `python main.py db start`, "
        "or set SPIDEY_TEST_DATABASE_URL"
    )


@pytest.fixture(scope="session")
def migrated(dsn: str) -> str:
    """Bring the schema to head once per session.

    Alembic's env resolves a URL itself only when one is not already set, so setting it
    here is what points migrations at the test database rather than the configured one.
    """
    from alembic import command
    from alembic.config import Config as AlembicConfig

    alembic_config = AlembicConfig(str(paths.ALEMBIC_INI))
    alembic_config.set_main_option("script_location", str(paths.MIGRATIONS_DIR))
    alembic_config.set_main_option("sqlalchemy.url", dsn)
    command.upgrade(alembic_config, "head")
    return dsn


@pytest_asyncio.fixture
async def engine(settings: Settings, migrated: str):
    """The application's engine, pointed at the test database."""
    yield db.configure(settings, url=migrated)
    await db.dispose()


@pytest_asyncio.fixture
async def clean(engine):
    """Empty every table before each test, so each starts from a known state.

    Truncated rather than isolated in a rolled-back transaction on purpose: the queue
    tests need separate connections to observe each other's committed rows, which a single
    never-committed transaction cannot express.
    """
    async with engine.begin() as connection:
        names = ", ".join(f'"{table.name}"' for table in reversed(Base.metadata.sorted_tables))
        await connection.execute(text(f"TRUNCATE {names} RESTART IDENTITY CASCADE"))


@pytest_asyncio.fixture
async def session(clean):
    """A session on the test database, committed on exit like production sessions."""
    async with db.session() as db_session:
        yield db_session
