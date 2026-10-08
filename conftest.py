"""Shared test fixtures.

Living at the project root also places the repository root on ``sys.path``, so
tests can ``import app`` without an installed package or a src layout.
"""

from __future__ import annotations

import os

import pytest
import pytest_asyncio
from sqlalchemy import text

from app import config, db, dbprovider, paths
from app.config import Settings
from app.models import Base

# The HTTP fixture server keeps its fixtures next to it, but pytest only collects fixtures declared
# in a conftest, so they are re-exported here for the tests that need a real server.
from tests.fakes.server import server, staging  # noqa: F401  (re-exported as fixtures)

FAKE_SECRETS: dict[str, str] = {
    "tg_api_id": "123456",
    "tg_api_hash": "0" * 32,
    "tg_bot_token": "123456:FAKE-TOKEN",
    "db_password": "fake-password",
}


@pytest.fixture
def fake_secrets() -> dict[str, str]:
    """Minimal valid secrets.

    Tests pass these explicitly rather than relying on a developer's real ``.env``,
    so the suite is deterministic and never touches real credentials.
    """
    return dict(FAKE_SECRETS)


# ── database fixtures, shared by the integration and acceptance layers ──
#
# Root-level rather than inside one directory, because both layers need a real Postgres:
# the integration tests exercise the SQL directly, and the acceptance tests drive handlers
# that write to it. Only a database is required — the Telegram credentials here are
# synthesised, so the suite runs on a checkout whose .env was never filled in.


@pytest.fixture(scope="session")
def settings() -> Settings:
    """Settings that need only a database.

    ``load_env_file`` is called inside a snapshot of the environment. It is what picks up a
    local password, but it writes into ``os.environ`` for the whole process, which would
    hand the *unit* suite a developer's real credentials and break the tests that assert on
    absent secrets. Snapshotting keeps the benefit without the leak.
    """
    snapshot = dict(os.environ)
    try:
        config.load_env_file()
        return config.build_settings(
            tg_api_id="1",
            tg_api_hash="a" * 32,
            tg_bot_token="1:fake",
        )
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
    """The layered resolver from the plan: explicit test DSN, then the bundled cluster.

    Skips rather than fails when neither exists, so a checkout with no Postgres still has a
    green unit suite instead of a wall of connection errors.
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
