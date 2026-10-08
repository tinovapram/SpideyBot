"""Local PostgreSQL without Docker.

Uses the bundled binaries from ``postgresql-binaries``, so a fresh checkout runs
with no installer, no Windows service and no container::

    python main.py db init
    python main.py db start
    python main.py db migrate
    python main.py db status
    python main.py db stop

The cluster listens on 127.0.0.1 only, on the non-default port from
``config/default.toml``, so it cannot clash with a real PostgreSQL install. It is
one of three supported providers (plan D3) — the application itself never learns
which one is in use, because it only ever receives a DSN.
"""

from __future__ import annotations

import os
import secrets
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from app import paths
from app.config import Settings, build_dsn

PASSWORD_FILE = paths.DATA_DIR / "pgpassword"
LOG_FILE = paths.PGDATA_DIR / "server.log"

_CONF_MARKER = "# ── spideybot managed block (rewritten on every start) ──"
_STARTUP_TIMEOUT_SECONDS = 30.0


class DbProviderError(RuntimeError):
    """A local-cluster operation could not be completed."""


@dataclass(frozen=True)
class ClusterState:
    initialised: bool
    running: bool
    ready: bool
    port: int
    data_dir: Path


# ── bundled binaries ────────────────────────────────────────────────────


def bin_dir() -> Path:
    """Directory holding the bundled PostgreSQL executables."""
    import postgresql_binaries

    return postgresql_binaries.bin()


def _exe(name: str) -> Path:
    suffix = ".exe" if os.name == "nt" else ""
    path = bin_dir() / f"{name}{suffix}"
    if not path.exists():
        raise DbProviderError(f"{name} is missing from {bin_dir()}")
    return path


def _run(
    args: list[os.PathLike[str] | str],
    *,
    password: str | None = None,
    check: bool = True,
    capture: bool = True,
) -> subprocess.CompletedProcess[str]:
    """Run a PostgreSQL client tool.

    ``capture=False`` is required for anything that *spawns a long-lived child*
    (notably ``pg_ctl start``): the server inherits the pipe handles, so reading
    them would block until the server exits — that is, forever.
    """
    env = dict(os.environ)
    if password is not None:
        env["PGPASSWORD"] = password
    env.setdefault("PGCLIENTENCODING", "UTF8")
    common = {
        "env": env,
        # Never let a stray prompt hang a headless start.
        "stdin": subprocess.DEVNULL,
    }
    if capture:
        result = subprocess.run([str(a) for a in args], capture_output=True, text=True, **common)
    else:
        result = subprocess.run(
            [str(a) for a in args],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            **common,
        )
    if check and result.returncode != 0:
        detail = ((result.stderr or result.stdout) or "").strip()
        raise DbProviderError(f"{Path(str(args[0])).name} failed: {detail}")
    return result


def _log_tail(lines: int = 8) -> str:
    """The tail of the server log, for error messages."""
    try:
        content = LOG_FILE.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return "(no server log)"
    return "\n".join(content[-lines:])


# ── the generated password ──────────────────────────────────────────────


def local_password() -> str | None:
    """The password generated for the bundled cluster, if one exists."""
    try:
        value = PASSWORD_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return value or None


def _store_local_password(password: str) -> None:
    PASSWORD_FILE.parent.mkdir(parents=True, exist_ok=True)
    PASSWORD_FILE.write_text(password + "\n", encoding="utf-8")
    if os.name == "posix":
        PASSWORD_FILE.chmod(0o600)


# ── cluster state ───────────────────────────────────────────────────────


def is_initialised() -> bool:
    return (paths.PGDATA_DIR / "PG_VERSION").exists()


def is_running() -> bool:
    if not is_initialised():
        return False
    result = _run([_exe("pg_ctl"), "-D", paths.PGDATA_DIR, "status"], check=False)
    return result.returncode == 0


def is_ready(port: int) -> bool:
    result = _run([_exe("pg_isready"), "-h", "127.0.0.1", "-p", str(port), "-q"], check=False)
    return result.returncode == 0


def status(port: int) -> ClusterState:
    initialised = is_initialised()
    return ClusterState(
        initialised=initialised,
        running=is_running(),
        ready=initialised and is_ready(port),
        port=port,
        data_dir=paths.PGDATA_DIR,
    )


# ── lifecycle ───────────────────────────────────────────────────────────


def _apply_managed_settings(port: int) -> None:
    """Pin the port and loopback binding, replacing any previous block.

    Written into postgresql.conf rather than passed to ``pg_ctl -o`` so there is
    no shell quoting to get wrong, and so ``pg_ctl`` alone starts a correct server.
    """
    conf = paths.PGDATA_DIR / "postgresql.conf"
    original = conf.read_text(encoding="utf-8", errors="replace")
    head = original.split(_CONF_MARKER)[0].rstrip()
    conf.write_text(
        f"{head}\n\n{_CONF_MARKER}\n"
        f"port = {port}\n"
        "listen_addresses = '127.0.0.1'\n"
        "max_connections = 50\n",
        encoding="utf-8",
    )


def init_cluster(settings: Settings, *, force: bool = False) -> str:
    """Create the cluster and return the generated password.

    Refuses to touch an existing cluster unless ``force`` is set, because that
    directory holds every session and job the bot has recorded.
    """
    if is_initialised() and not force:
        raise DbProviderError(
            f"{paths.PGDATA_DIR} already holds a cluster; use `db reset --force` to rebuild it"
        )
    if is_initialised() and is_running():
        raise DbProviderError("cluster is running; stop it before re-initialising")

    if force and paths.PGDATA_DIR.exists():
        shutil.rmtree(paths.PGDATA_DIR)
    # initdb creates the data directory itself. Creating it here first and writing
    # the password file inside is what made initdb refuse with
    # "directory exists but is not empty".
    paths.DATA_DIR.mkdir(parents=True, exist_ok=True)

    password = secrets.token_urlsafe(24)
    pwfile = paths.DATA_DIR / "initdb-pwfile"
    pwfile.write_text(password + "\n", encoding="utf-8")
    try:
        _run(
            [
                _exe("initdb"),
                "-D",
                paths.PGDATA_DIR,
                "-U",
                settings.db.user,
                "-E",
                "UTF8",
                "--locale=C",
                "--auth-local=trust",
                "--auth-host=scram-sha-256",
                f"--pwfile={pwfile}",
            ]
        )
    finally:
        pwfile.unlink(missing_ok=True)

    _store_local_password(password)
    _apply_managed_settings(settings.db.port)
    return password


def start(settings: Settings, *, timeout: float = _STARTUP_TIMEOUT_SECONDS) -> ClusterState:
    """Start the cluster if it is not already up. Idempotent.

    The database named in the config is created on first start, so the operator
    never has to remember ``createdb``.
    """
    if not is_initialised():
        raise DbProviderError(
            f"no cluster at {paths.PGDATA_DIR}; run `python main.py db init` first"
        )

    _apply_managed_settings(settings.db.port)

    if not is_running():
        paths.DATA_DIR.mkdir(parents=True, exist_ok=True)
        # Uncaptured on purpose: see _run. pg_ctl's own -l log holds the detail.
        started = _run(
            [_exe("pg_ctl"), "-D", paths.PGDATA_DIR, "-l", LOG_FILE, "-w", "start"],
            capture=False,
            check=False,
        )
        if started.returncode != 0:
            raise DbProviderError(
                f"pg_ctl could not start the server; see {LOG_FILE}\n{_log_tail()}"
            )

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if is_ready(settings.db.port):
            _ensure_database(settings)
            return status(settings.db.port)
        time.sleep(0.25)

    raise DbProviderError(f"server did not become ready within {timeout:.0f}s; see {LOG_FILE}")


def stop() -> None:
    """Stop the cluster gracefully. Idempotent."""
    if not is_running():
        return
    _run([_exe("pg_ctl"), "-D", paths.PGDATA_DIR, "-m", "fast", "-w", "stop"])


def reset(settings: Settings, *, force: bool = False) -> None:
    """Stop the cluster and delete its data directory."""
    if not force:
        raise DbProviderError("`db reset` deletes all data; pass --force to confirm")
    stop()
    shutil.rmtree(paths.PGDATA_DIR, ignore_errors=True)
    PASSWORD_FILE.unlink(missing_ok=True)


def _ensure_database(settings: Settings) -> None:
    password = _cluster_password(settings)
    base = ["-h", "127.0.0.1", "-p", str(settings.db.port), "-U", settings.db.user]
    exists = _run(
        [
            _exe("psql"),
            *base,
            "-d",
            "postgres",
            "-tAc",
            f"SELECT 1 FROM pg_database WHERE datname = '{settings.db.name}'",
        ],
        password=password,
    )
    if exists.stdout.strip() == "1":
        return
    _run(
        [_exe("createdb"), *base, "-O", settings.db.user, settings.db.name],
        password=password,
    )


# ── DSN resolution ──────────────────────────────────────────────────────


def _cluster_password(settings: Settings) -> str:
    if settings.db_password is not None and settings.db_password.get_secret_value():
        return settings.db_password.get_secret_value()
    generated = local_password()
    if generated is not None:
        return generated
    raise DbProviderError(
        "no database password: set SPIDEY_DB_PASSWORD in .env, "
        "or run `python main.py db init` to create a local cluster"
    )


def resolve_dsn(settings: Settings) -> str:
    """The DSN to use at runtime.

    Prefers an explicitly configured password, then falls back to the bundled
    cluster's generated one — which is what lets a fresh checkout run untouched.
    """
    return build_dsn(settings.db, _cluster_password(settings))
