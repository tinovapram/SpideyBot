"""
Centralized project paths.

Every on-disk location is derived from the project root so the bot works
regardless of the process working directory (Docker, systemd, ``python -m``).
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path

# Project root = parent of this package (`utils/`).
PROJECT_ROOT = Path(__file__).resolve().parents[1]

DOWNLOADS_DIR = PROJECT_ROOT / "downloads"
DATA_DIR = PROJECT_ROOT / "data"
SESSIONS_DIR = PROJECT_ROOT / "user_sessions"
CONFIG_DIR = PROJECT_ROOT / "config"
RUNTIME_CONFIG_DIR = CONFIG_DIR / "runtime"
READY_FILE = DATA_DIR / "ready"

DB_PATH = DATA_DIR / "bot_database.db"

GALLERYDL_USER_CONFIG = CONFIG_DIR / "gallery-dl.json"
GALLERYDL_RUNTIME_CONFIG = RUNTIME_CONFIG_DIR / "gallery-dl-runtime.json"
YTDLP_CONFIG = CONFIG_DIR / "yt-dlp.conf"


def ensure_directories() -> None:
    """Create all runtime directories once at startup."""
    for directory in (DOWNLOADS_DIR, DATA_DIR, SESSIONS_DIR, RUNTIME_CONFIG_DIR):
        directory.mkdir(parents=True, exist_ok=True)
        try:
            directory.chmod(0o700)
        except OSError:
            pass


_STAGING_DIR_RE = re.compile(r"^(?:\d+|tg_\d+)$")


def clear_staging() -> None:
    """Delete leftover download staging, keeping ``DOWNLOADS_DIR`` itself.

    Flows write under ``downloads/<user id>/…`` and ``downloads/tg_<user id>/``.
    Only those names are removed, so the directory can stay bind-mounted and
    unrelated files in it are never touched.
    """
    DOWNLOADS_DIR.mkdir(parents=True, exist_ok=True)
    for entry in DOWNLOADS_DIR.iterdir():
        if not _STAGING_DIR_RE.match(entry.name):
            continue
        if entry.is_dir():
            shutil.rmtree(entry, ignore_errors=True)
        else:
            try:
                entry.unlink()
            except OSError:
                pass
