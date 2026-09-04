"""
Application configuration.

All environment-driven settings live here as module-level constants, loaded
once at import time. Helper functions expose tier-aware limits.
"""

from __future__ import annotations

import os

from dotenv import load_dotenv

load_dotenv()


class ConfigError(Exception):
    """Raised when required configuration is missing or malformed."""


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


# ── Telegram ──────────────────────────────────────────────────────

TG_API_ID = os.getenv("TG_API_ID")
TG_API_HASH = os.getenv("TG_API_HASH")
TG_BOT_TOKEN = os.getenv("TG_BOT_TOKEN")

SESSION_ENCRYPT_KEY = os.getenv("SESSION_ENCRYPT_KEY", "")


def validate_telegram_config() -> int:
    """Validate Telegram credentials and return ``api_id`` as an int.

    Raises:
        ConfigError: if any required value is missing or malformed.
    """
    if not (TG_API_ID and TG_API_HASH and TG_BOT_TOKEN):
        raise ConfigError(
            "Missing Telegram configuration: "
            "TG_API_ID, TG_API_HASH and TG_BOT_TOKEN are all required."
        )
    try:
        return int(TG_API_ID)
    except (TypeError, ValueError) as exc:
        raise ConfigError("TG_API_ID must be a valid integer.") from exc


# ── TeraBox ───────────────────────────────────────────────────────

TERABOX_COOKIE = os.getenv("TERABOX_COOKIE")
TERABOX_JSTOKEN = os.getenv("TERABOX_JSTOKEN")
TERABOX_BDSTOKEN = os.getenv("TERABOX_BDSTOKEN")

# ── Reddit (fallback chain) ───────────────────────────────────────

REDDIT_FALLBACK_CLIENT_ID = os.getenv(
    "REDDIT_FALLBACK_CLIENT_ID", os.getenv("REDDIT_GDL_CLIENT_ID", "")
)
REDDIT_FALLBACK_CLIENT_SECRET = os.getenv(
    "REDDIT_FALLBACK_CLIENT_SECRET", os.getenv("REDDIT_GDL_CLIENT_SECRET", "")
)
REDDIT_FALLBACK_REFRESH_TOKEN = os.getenv(
    "REDDIT_FALLBACK_REFRESH_TOKEN", os.getenv("REDDIT_GDL_REFRESH_TOKEN", "")
)

GDL_REDDIT_CLIENT_ID = os.getenv("GDL_REDDIT_CLIENT_ID", REDDIT_FALLBACK_CLIENT_ID)
GDL_REDDIT_CLIENT_SECRET = os.getenv("GDL_REDDIT_CLIENT_SECRET", REDDIT_FALLBACK_CLIENT_SECRET)
GDL_REDDIT_REFRESH_TOKEN = os.getenv("GDL_REDDIT_REFRESH_TOKEN", REDDIT_FALLBACK_REFRESH_TOKEN)

REDDIT_PRAW_CLIENT_ID = os.getenv("REDDIT_PRAW_CLIENT_ID", REDDIT_FALLBACK_CLIENT_ID)
REDDIT_PRAW_CLIENT_SECRET = os.getenv("REDDIT_PRAW_CLIENT_SECRET", REDDIT_FALLBACK_CLIENT_SECRET)
REDDIT_PRAW_REFRESH_TOKEN = os.getenv("REDDIT_PRAW_REFRESH_TOKEN", REDDIT_FALLBACK_REFRESH_TOKEN)

# ── Download management ───────────────────────────────────────────

MAX_CONCURRENT_DOWNLOADS = _int_env("MAX_CONCURRENT_DOWNLOADS", 20)
MAX_CONCURRENT_FREE_TOTAL = _int_env("MAX_CONCURRENT_FREE_TOTAL", 10)

SIZE_LIMIT_FREE = _int_env("MAX_SIZE_FREE_MB", 100) * 1024 * 1024
SIZE_LIMIT_PREMIUM = _int_env("MAX_SIZE_PREMIUM_MB", 1000) * 1024 * 1024

CONCURRENT_LIMIT_FREE = _int_env("MAX_CONCURRENT_FREE", 1)
CONCURRENT_LIMIT_PREMIUM = _int_env("MAX_CONCURRENT_PREMIUM", 5)

# ── Admin ─────────────────────────────────────────────────────────

ADMIN_IDS: list[int] = [
    int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip().isdigit()
]

# ── Constants ─────────────────────────────────────────────────────

TERABOX_DOMAINS = ("terabox", "nephobox", "dubox", "1024tera", "teraboxapp")


# ── Helpers ───────────────────────────────────────────────────────

def is_terabox_url(url: str) -> bool:
    """Return True when *url* belongs to a TeraBox domain."""
    lowered = url.lower()
    return any(domain in lowered for domain in TERABOX_DOMAINS)


def _format_size_limit(size_bytes: float) -> str:
    mb = size_bytes / (1024 * 1024)
    return f"{mb / 1024:.0f}GB" if mb >= 1000 else f"{mb:.0f}MB"


def get_size_limit(is_premium: bool, is_admin: bool) -> tuple[float, str]:
    """Return ``(max_size_bytes, human_label)`` for a user tier."""
    if is_admin:
        return float("inf"), "Unlimited"
    if is_premium:
        return float(SIZE_LIMIT_PREMIUM), _format_size_limit(SIZE_LIMIT_PREMIUM)
    return float(SIZE_LIMIT_FREE), _format_size_limit(SIZE_LIMIT_FREE)


def get_concurrent_limit(is_premium: bool) -> int:
    """Return the per-user concurrent download limit for a tier."""
    return CONCURRENT_LIMIT_PREMIUM if is_premium else CONCURRENT_LIMIT_FREE
