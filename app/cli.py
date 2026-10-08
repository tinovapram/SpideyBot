"""Command-line entry points.

    python main.py db init|start|stop|status|migrate|reset
    python main.py run
"""

from __future__ import annotations

import argparse
import sys

from app import config, dbprovider, paths


def _settings() -> config.Settings:
    config.load_env_file()
    config.reset_settings_cache()
    return config.get_settings()


def _cmd_db_init(args: argparse.Namespace) -> int:
    settings = _settings()
    password = dbprovider.init_cluster(settings, force=args.force)
    print(f"cluster created at {paths.PGDATA_DIR}")
    print(f"generated password stored in {dbprovider.PASSWORD_FILE}")
    print(f"port {settings.db.port}, listening on 127.0.0.1 only")
    print(f"password length: {len(password)} characters (not printed)")
    return 0


def _cmd_db_start(args: argparse.Namespace) -> int:
    state = dbprovider.start(_settings())
    print(f"postgres ready on 127.0.0.1:{state.port} (data: {state.data_dir})")
    return 0


def _cmd_db_stop(args: argparse.Namespace) -> int:
    dbprovider.stop()
    print("postgres stopped")
    return 0


def _cmd_db_status(args: argparse.Namespace) -> int:
    settings = _settings()
    state = dbprovider.status(settings.db.port)
    print(f"initialised : {state.initialised}")
    print(f"running     : {state.running}")
    print(f"ready       : {state.ready}")
    print(f"port        : {state.port}")
    print(f"data dir    : {state.data_dir}")
    print(f"dsn         : {config.build_dsn_safe(settings.db)}")
    return 0


def _cmd_db_migrate(args: argparse.Namespace) -> int:
    from alembic import command
    from alembic.config import Config as AlembicConfig

    settings = _settings()
    alembic_config = AlembicConfig(str(paths.ALEMBIC_INI))
    alembic_config.set_main_option("script_location", str(paths.MIGRATIONS_DIR))
    alembic_config.set_main_option("sqlalchemy.url", dbprovider.resolve_dsn(settings))

    command.upgrade(alembic_config, args.revision)
    print(f"migrated to {args.revision}")
    return 0


def _cmd_db_reset(args: argparse.Namespace) -> int:
    dbprovider.reset(_settings(), force=args.force)
    print("cluster stopped and removed")
    return 0


def _cmd_run(args: argparse.Namespace) -> int:
    from app.bot import run

    return run()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="spideybot", description="SpideyBot")
    sub = parser.add_subparsers(dest="command")

    db = sub.add_parser("db", help="manage the local PostgreSQL cluster")
    db_sub = db.add_subparsers(dest="db_command", required=True)

    p_init = db_sub.add_parser("init", help="create the cluster and generate a password")
    p_init.add_argument("--force", action="store_true", help="rebuild an existing cluster")
    p_init.set_defaults(func=_cmd_db_init)

    db_sub.add_parser("start", help="start the cluster (idempotent)").set_defaults(
        func=_cmd_db_start
    )
    db_sub.add_parser("stop", help="stop the cluster").set_defaults(func=_cmd_db_stop)
    db_sub.add_parser("status", help="show cluster state").set_defaults(func=_cmd_db_status)

    p_migrate = db_sub.add_parser("migrate", help="apply Alembic migrations")
    p_migrate.add_argument("revision", nargs="?", default="head")
    p_migrate.set_defaults(func=_cmd_db_migrate)

    p_reset = db_sub.add_parser("reset", help="delete the cluster and all its data")
    p_reset.add_argument("--force", action="store_true", help="required: confirms data loss")
    p_reset.set_defaults(func=_cmd_db_reset)

    sub.add_parser("run", help="start the bot").set_defaults(func=_cmd_run)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "command", None):
        parser.print_help()
        return 2
    try:
        return int(args.func(args))
    except (dbprovider.DbProviderError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
