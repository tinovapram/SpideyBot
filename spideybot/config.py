"""
SpideyBot — Configuration Module.

Centralizes all environment variable loading, validation, and application constants.
All configuration is loaded once at import time and made available as module-level attributes.
"""

import os
import re
import logging
from dotenv import load_dotenv

logger = logging.getLogger(__name__)

# Load environment variables
load_dotenv()

# ─── Telegram Configuration ─────────────────────────────────────────

TG_API_ID = os.getenv("TG_API_ID")
TG_API_HASH = os.getenv("TG_API_HASH")
TG_BOT_TOKEN = os.getenv("TG_BOT_TOKEN")

def validate_telegram_config():
    """Validate that required Telegram config is present and well-formed."""
    if not TG_API_ID or not TG_API_HASH or not TG_BOT_TOKEN:
        logger.error(
            "Missing configuration! Please ensure TG_API_ID, TG_API_HASH, "
            "and TG_BOT_TOKEN are set in your .env file."
        )
        raise SystemExit(1)

    try:
        api_id = int(TG_API_ID)
    except ValueError:
        logger.error("TG_API_ID must be a valid integer.")
        raise SystemExit(1)

    return api_id

# ─── TeraBox Configuration ──────────────────────────────────────────

TERABOX_COOKIE = os.getenv("TERABOX_COOKIE")
TERABOX_JSTOKEN = os.getenv("TERABOX_JSTOKEN")
TERABOX_BDSTOKEN = os.getenv("TERABOX_BDSTOKEN")

# ─── Reddit / Gallery-dl Configuration ──────────────────────────────

# General / Fallback credentials
REDDIT_CLIENT_ID = os.getenv("REDDIT_CLIENT_ID", "")
REDDIT_CLIENT_SECRET = os.getenv("REDDIT_CLIENT_SECRET", "")
REDDIT_REFRESH_TOKEN = os.getenv("REDDIT_REFRESH_TOKEN", "")

# Specific to gallery-dl Reddit extractor
GDL_REDDIT_CLIENT_ID = os.getenv("GDL_REDDIT_CLIENT_ID", REDDIT_CLIENT_ID)
GDL_REDDIT_CLIENT_SECRET = os.getenv("GDL_REDDIT_CLIENT_SECRET", REDDIT_CLIENT_SECRET)
GDL_REDDIT_REFRESH_TOKEN = os.getenv("GDL_REDDIT_REFRESH_TOKEN", REDDIT_REFRESH_TOKEN)

# Specific to RedditDownloader (PRAW)
REDDIT_PRAW_CLIENT_ID = os.getenv("REDDIT_PRAW_CLIENT_ID", REDDIT_CLIENT_ID)
REDDIT_PRAW_CLIENT_SECRET = os.getenv("REDDIT_PRAW_CLIENT_SECRET", REDDIT_CLIENT_SECRET)
REDDIT_PRAW_REFRESH_TOKEN = os.getenv("REDDIT_PRAW_REFRESH_TOKEN", REDDIT_REFRESH_TOKEN)

# ─── Download Management ────────────────────────────────────────────

_max_concurrent_str = os.getenv("MAX_CONCURRENT_DOWNLOADS", "20")
try:
    MAX_CONCURRENT_DOWNLOADS = int(_max_concurrent_str)
except ValueError:
    MAX_CONCURRENT_DOWNLOADS = 20

_max_concurrent_free_total_str = os.getenv("MAX_CONCURRENT_FREE_TOTAL", "10")
try:
    MAX_CONCURRENT_FREE_TOTAL = int(_max_concurrent_free_total_str)
except ValueError:
    MAX_CONCURRENT_FREE_TOTAL = 10

# ─── Admin Configuration ────────────────────────────────────────────

ADMIN_IDS = []
_admin_ids_str = os.getenv("ADMIN_IDS", "")
if _admin_ids_str:
    for x in _admin_ids_str.split(","):
        try:
            ADMIN_IDS.append(int(x.strip()))
        except ValueError:
            pass

# ─── Constants ───────────────────────────────────────────────────────

TERABOX_DOMAINS = ["terabox", "nephobox", "dubox", "1024tera", "teraboxapp"]

# Size limits in bytes per user tier (configurable via env, values in MB)
_size_free_mb = os.getenv("MAX_SIZE_FREE_MB", "100")
_size_premium_mb = os.getenv("MAX_SIZE_PREMIUM_MB", "1000")
try:
    SIZE_LIMIT_FREE = int(_size_free_mb) * 1024 * 1024
except ValueError:
    SIZE_LIMIT_FREE = 100 * 1024 * 1024       # 100 MB default

try:
    SIZE_LIMIT_PREMIUM = int(_size_premium_mb) * 1024 * 1024
except ValueError:
    SIZE_LIMIT_PREMIUM = 1000 * 1024 * 1024   # 1 GB default

# Concurrent download limits per user tier (configurable via env)
_concurrent_free = os.getenv("MAX_CONCURRENT_FREE", "1")
_concurrent_premium = os.getenv("MAX_CONCURRENT_PREMIUM", "5")
try:
    CONCURRENT_LIMIT_FREE = int(_concurrent_free)
except ValueError:
    CONCURRENT_LIMIT_FREE = 1

try:
    CONCURRENT_LIMIT_PREMIUM = int(_concurrent_premium)
except ValueError:
    CONCURRENT_LIMIT_PREMIUM = 5

# ─── Helper Functions ────────────────────────────────────────────────

def is_terabox_url(url: str) -> bool:
    """Check if a URL belongs to a TeraBox domain."""
    url_lower = url.lower()
    return any(domain in url_lower for domain in TERABOX_DOMAINS)


def _format_size_limit(size_bytes: int) -> str:
    """Format a byte size limit into a human-readable string (MB or GB)."""
    mb = size_bytes / (1024 * 1024)
    if mb >= 1000:
        return f"{mb / 1024:.0f}GB"
    return f"{mb:.0f}MB"


def get_size_limit(is_premium: bool, is_admin: bool) -> tuple:
    """
    Get the download size limit for a user tier.

    Returns:
        (max_size_bytes, limit_str): The size limit in bytes and a human-readable string.
        max_size_bytes is float('inf') for admins.
    """
    if is_admin:
        return float('inf'), "Unlimited"
    elif is_premium:
        return SIZE_LIMIT_PREMIUM, _format_size_limit(SIZE_LIMIT_PREMIUM)
    else:
        return SIZE_LIMIT_FREE, _format_size_limit(SIZE_LIMIT_FREE)


def get_concurrent_limit(is_premium: bool) -> int:
    """Get the concurrent download limit for a user tier."""
    return CONCURRENT_LIMIT_PREMIUM if is_premium else CONCURRENT_LIMIT_FREE
