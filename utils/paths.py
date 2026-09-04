"""
Centralized project paths.

Every on-disk location is derived from the project root so the bot works
regardless of the process working directory (Docker, systemd, ``python -m``).
"""

from __future__ import annotations

from pathlib import Path

# Project root = parent of this package (`utils/`).
PROJECT_ROOT = Path(__file__).resolve().parents[1]

DOWNLOADS_DIR = PROJECT_ROOT / "downloads"
DATA_DIR = PROJECT_ROOT / "data"
SESSIONS_DIR = PROJECT_ROOT / "user_sessions"
CONFIG_DIR = PROJECT_ROOT / "config"
RUNTIME_CONFIG_DIR = CONFIG_DIR / "runtime"

DB_PATH = DATA_DIR / "bot_database.db"

GALLERYDL_USER_CONFIG = CONFIG_DIR / "gallery-dl.json"
GALLERYDL_RUNTIME_CONFIG = RUNTIME_CONFIG_DIR / "gallery-dl-runtime.json"
YTDLP_CONFIG = CONFIG_DIR / "yt-dlp.conf"


def ensure_directories() -> None:
    """Create all runtime directories once at startup."""
    for directory in (DOWNLOADS_DIR, DATA_DIR, SESSIONS_DIR, RUNTIME_CONFIG_DIR):
        directory.mkdir(parents=True, exist_ok=True)
