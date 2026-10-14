"""Alembic environment.

The database URL is resolved at runtime rather than read from ``alembic.ini``, so
the bundled local cluster works with no configuration at all, and an external
database only needs ``SPIDEY_DB_PASSWORD``.
"""

from __future__ import annotations

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.ext.asyncio import async_engine_from_config

from app import config as app_config
from app import dbprovider
from app.models import Base

config = context.config

if config.config_file_name is not None:
    # `disable_existing_loggers=False` because the default is `True`, and it means "silence every
    # logger that already exists" — including this application's. Running a migration in-process
    # (the test suite does; a future auto-migrate would) therefore switched off all logging for the
    # rest of the process, so a failure after a migration was reported to nobody. Alembic's own
    # loggers are still configured from `alembic.ini`; only the silencing is dropped.
    fileConfig(config.config_file_name, disable_existing_loggers=False)

if not config.get_main_option("sqlalchemy.url", None):
    app_config.load_env_file()
    config.set_main_option("sqlalchemy.url", dbprovider.resolve_dsn(app_config.get_settings()))

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: object) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        # Catch type changes, not just added/dropped columns.
        compare_type=True,
        compare_server_default=True,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
