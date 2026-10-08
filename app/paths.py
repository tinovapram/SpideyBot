"""Filesystem layout.

All runtime locations derive from the project root, so the bot behaves the same
from a checkout, a container or a service directory. Nothing here needs Docker.

This module owns *where* things live; ``app.config`` owns *policy* (limits,
intervals, retention). Keeping the two apart is why there is no ``temp_root``
setting — a configurable temp root would just be a second source of truth.
"""

from __future__ import annotations

import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

CONFIG_DIR = PROJECT_ROOT / "config"
DATA_DIR = Path(os.environ.get("SPIDEY_DATA_DIR") or (PROJECT_ROOT / "data"))

# Credentials. Plaintext by design (plan D7-D9), so the directory permissions are
# the only thing protecting them — hence insecure_secret_dirs() below.
SESSIONS_DIR = DATA_DIR / "sessions"
TEST_SESSIONS_DIR = SESSIONS_DIR / "test"
COOKIES_DIR = DATA_DIR / "cookies"

# The bot's own session is not a user credential, so it lives outside SESSIONS_DIR:
# /logout and the reaper operate on that directory, and the bot must never be able
# to delete its own session by accident.
BOT_DIR = DATA_DIR / "bot"

# Everything that holds a credential. Created 0700: see ensure_runtime_dirs().
SECRET_DIRS = (SESSIONS_DIR, TEST_SESSIONS_DIR, COOKIES_DIR, BOT_DIR)

TMP_DIR = DATA_DIR / "tmp"
DOWNLOADS_DIR = DATA_DIR / "downloads"
PGDATA_DIR = DATA_DIR / "pgdata"

MIGRATIONS_DIR = PROJECT_ROOT / "migrations"
ALEMBIC_INI = PROJECT_ROOT / "alembic.ini"

DEFAULT_TOML = CONFIG_DIR / "default.toml"
LOCAL_TOML = CONFIG_DIR / "local.toml"
ENV_FILE = PROJECT_ROOT / ".env"


def ensure_runtime_dirs() -> None:
    """Create the runtime directories and tighten the credential ones. Safe to repeat.

    The credential directories are set to 0700 because the session and cookie files
    inside them are plaintext by design (D7-D9), which makes the filesystem the only
    thing protecting them (R10). A default ``mkdir`` under a typical umask yields 0755 —
    readable by every other account on the host — which is exactly what
    :func:`insecure_secret_dirs` then reports at startup.
    """
    for path in (
        DATA_DIR,
        SESSIONS_DIR,
        TEST_SESSIONS_DIR,
        COOKIES_DIR,
        BOT_DIR,
        TMP_DIR,
        DOWNLOADS_DIR,
    ):
        path.mkdir(parents=True, exist_ok=True)

    for path in SECRET_DIRS:
        _restrict(path)


def _restrict(path: Path) -> None:
    """Remove group and other access from a credential directory.

    Best effort by design: a filesystem that refuses ``chmod`` must not stop the bot from
    starting, and :func:`insecure_secret_dirs` still reports the directory either way.
    """
    if os.name != "posix":
        return
    try:
        path.chmod(0o700)
    except OSError:
        pass


def session_path(user_id: int, *, test_mode: bool = False) -> Path:
    """Session file path *without* the ``.session`` suffix, which Kurigram appends.

    Live-test sessions live in their own directory, so a test run can never
    clobber a real user's session.
    """
    directory = TEST_SESSIONS_DIR if test_mode else SESSIONS_DIR
    return directory / str(user_id)


def cookie_path(user_id: int) -> Path:
    return COOKIES_DIR / str(user_id)


def session_file(user_id: int, *, test_mode: bool = False) -> Path:
    """The file Kurigram actually writes.

    ``session_path`` deliberately omits the suffix because that is what the library
    wants as its ``name``; this is the on-disk result, for existence checks.
    """
    return session_path(user_id, test_mode=test_mode).with_suffix(".session")


def job_tmp_dir(job_id: int) -> Path:
    return TMP_DIR / str(job_id)


def _is_loose(path: Path) -> bool:
    """True when POSIX group/other bits grant access.

    Windows ACLs are deliberately not modelled; on Windows this always returns
    False and the startup warning stays silent.
    """
    if os.name != "posix":
        return False
    try:
        return bool(path.stat().st_mode & 0o077)
    except OSError:
        return False


def insecure_secret_dirs() -> list[Path]:
    """Credential directories readable beyond the service account.

    Checked at startup so the operator hears about it once, loudly, rather than
    discovering it after a leak.
    """
    return [p for p in (SESSIONS_DIR, COOKIES_DIR) if p.exists() and _is_loose(p)]
