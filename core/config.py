"""Application configuration (typed, pydantic-settings).

Every setting uses the ``SPIDEY_`` env prefix and is loaded once via the
cached :func:`get_settings`. Validation happens at first access, so a broken
``.env`` fails fast at startup.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

import structlog
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from utils import paths

logger = structlog.get_logger(__name__)

_ENV_PREFIX = "SPIDEY_"


class ConfigError(Exception):
    """Raised when required configuration is missing or malformed."""


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="SPIDEY_",
        env_file=paths.PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ── Telegram ─────────────────────────────────────────────────
    tg_api_id: int | None = None
    tg_api_hash: str | None = None
    tg_bot_token: str | None = None

    # ── Database ─────────────────────────────────────────────────
    # Required: a credential-bearing default used to point at a dev database.
    database_url: str = Field(min_length=1)

    # ── Security ─────────────────────────────────────────────────
    session_encrypt_key: str = ""

    # ── TeraBox ──────────────────────────────────────────────────
    terabox_cookies: str = ""  # single or multi-account, "|"-delimited
    terabox_jstoken: str = ""
    terabox_bdstoken: str = ""
    terabox_sign: str = ""
    terabox_timestamp: str = ""
    terabox_logid: str = ""
    terabox_transfer: str = "auto"  # auto | aria2 | segmented | single
    terabox_transfer_min_mb: int = 32
    terabox_segment_connections: int = 8
    terabox_aria2_connections: int = 16

    # ── Reddit ───────────────────────────────────────────────────
    reddit_fallback_client_id: str = ""
    reddit_fallback_client_secret: str = ""
    reddit_fallback_refresh_token: str = ""
    gdl_reddit_client_id: str = ""
    gdl_reddit_client_secret: str = ""
    gdl_reddit_refresh_token: str = ""
    reddit_praw_client_id: str = ""
    reddit_praw_client_secret: str = ""
    reddit_praw_refresh_token: str = ""
    gdl_cookies_from_browser: str = ""

    # ── Download management ──────────────────────────────────────
    max_concurrent_downloads: int = 20
    proxy_pool: str = ""  # comma-separated protocol://host:port entries

    # ── Queue / jobs ─────────────────────────────────────────────
    job_max_attempts: int = 3

    # ── Admin ────────────────────────────────────────────────────
    admin_ids: str = ""

    # ── Rate limiting ────────────────────────────────────────────
    rate_limit_per_minute: int = 12

    # ── Media preparation & send pacing (core/sendgate.py) ───────
    # The limited unit is **one prepared media** (one file the user asked
    # for), whether it is later sent in an album or on its own.
    media_concurrency: int = 5  # slots on the bot client; 0 = follow sessionless share
    media_per_minute: int = 60  # per client
    media_burst: int = 5  # media that may start at once
    send_chat_per_minute: int = 20  # message RPCs per chat
    send_burst: int = 5  # message RPCs that may leave at once
    flood_jitter_seconds: float = 2.0  # de-synchronises flood retries
    send_max_attempts: int = 4

    # ── Sessionless jobs (docs/DELIVERY_PIPELINE.md §8) ──────────
    sessionless_worker_slots: int = 5

    # ── User sessions (on-demand lifecycle) ──────────────────────
    session_max_live: int = 50
    # Idle timeout before a live client is disconnected (the session file is
    # kept, so /start reconnects). Per tier: free, pro, premium.
    session_idle_seconds: int = 1800  # free (also the fallback for unknown tiers)
    session_idle_pro_seconds: int = 43200  # 12 h
    session_idle_premium_seconds: int = 86400  # 24 h

    # ── Observability ───────────────────────────────────────────
    metrics_host: str = "0.0.0.0"
    metrics_port: int = 9090

    # ── Watchdogs (core/watchdog.py) ────────────────────────────
    # A blocked event loop is logged once it exceeds this lag.
    loop_lag_threshold_seconds: float = 2.0
    watchdog_interval_seconds: int = 30
    watchdog_grace_seconds: int = 60  # tolerate a short reconnect blip
    watchdog_max_reconnects: int = 5  # then exit so the container restarts

    # ── Browser (Camoufox) ──────────────────────────────────────
    camoufox_max_concurrency: int = 2
    camoufox_headless: str = ""  # "" | "virtual" | "1" | "0"

    # ── Bot behavior defaults ────────────────────────────────────
    default_timer_media: bool = True
    default_magic_word: str = "WOW"

    # ── Referral ─────────────────────────────────────────────────
    referral_daily_bonus: int = 10
    referral_bonus_days: int = 30

    # ── Tier: Free ───────────────────────────────────────────────
    tier_free_link_total_mb: int = 500
    tier_free_daily_downloads: int = 10
    tier_free_daily_mb: int = 1024
    tier_free_concurrent: int = 1
    tier_free_priority: float = 2.0
    # ponytail: "reject" (default) or "queue"; invalid values behave like reject
    tier_free_concurrent_mode: str = "reject"

    # ── Tier: Pro ────────────────────────────────────────────────
    tier_pro_link_total_mb: int = 10240
    tier_pro_daily_downloads: int = 50
    tier_pro_daily_mb: int = 25600
    tier_pro_concurrent: int = 2
    tier_pro_priority: float = 1.0
    tier_pro_concurrent_mode: str = "reject"

    # ── Tier: Premium ────────────────────────────────────────────
    tier_premium_daily_downloads: int = 200
    tier_premium_daily_mb: int = 102400
    tier_premium_concurrent: int = 3
    tier_premium_priority: float = 0.5
    tier_premium_concurrent_mode: str = "reject"

    def admin_id_list(self) -> list[int]:
        """Parse comma-separated ``admin_ids`` string into a list."""
        if not self.admin_ids:
            return []
        return [int(x) for x in self.admin_ids.split(",") if x.strip().isdigit()]

    # ── Helpers ──────────────────────────────────────────────────

    def validate_telegram(self) -> int:
        """Validate Telegram credentials and return ``api_id`` as an int."""
        if not (self.tg_api_id and self.tg_api_hash and self.tg_bot_token):
            raise ConfigError(
                "Missing Telegram configuration: SPIDEY_TG_API_ID, "
                "SPIDEY_TG_API_HASH and SPIDEY_TG_BOT_TOKEN are all required."
            )
        return int(self.tg_api_id)

    def terabox_account_cookies(self) -> list[str]:
        """Return one cookie string per TeraBox account to use."""
        return [p.strip() for p in self.terabox_cookies.split("|") if p.strip()]

    def media_slot_budget(self) -> int:
        """Media slots on the shared bot client.

        ``SPIDEY_MEDIA_CONCURRENCY=0`` follows the sessionless worker share, so
        the shared clients move together when an operator tunes one of them.
        """
        if self.media_concurrency > 0:
            return self.media_concurrency
        return max(1, self.sessionless_worker_slots)

    def session_idle_for(self, tier: str) -> int:
        """Idle timeout in seconds for *tier* (admins resolve to ``premium``)."""
        if tier == "premium":
            return max(1, self.session_idle_premium_seconds)
        if tier == "pro":
            return max(1, self.session_idle_pro_seconds)
        return max(1, self.session_idle_seconds)


def _known_env_keys() -> set[str]:
    """Return the env var names the settings model actually consumes."""
    return {f"{_ENV_PREFIX}{name.upper()}" for name in Settings.model_fields}


def _env_file_keys() -> set[str]:
    """Return keys defined in the configured ``.env`` file."""
    configured = Settings.model_config.get("env_file")
    if not configured:
        return set()
    candidates = configured if isinstance(configured, (list, tuple)) else [configured]
    path = next(
        (Path(entry) for entry in candidates if isinstance(entry, (str, Path))), None
    )
    if path is None or not path.is_file():
        return set()
    keys: set[str] = set()
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        keys.add(stripped.split("=", 1)[0].strip())
    return keys


def unknown_env_keys() -> list[str]:
    """Return ``SPIDEY_*`` keys nothing consumes — typos or removed options."""
    candidates = {key for key in os.environ if key.startswith(_ENV_PREFIX)}
    candidates |= {key for key in _env_file_keys() if key.startswith(_ENV_PREFIX)}
    return sorted(candidates - _known_env_keys())


@lru_cache
def get_settings() -> Settings:
    """Return the cached settings instance, warning about ignored keys."""
    settings = Settings()  # pyright: ignore[reportCallIssue]  # env/.env supply the values
    unknown = unknown_env_keys()
    if unknown:
        logger.warning(
            "Ignoring unknown SPIDEY_* settings — check for typos or stale options",
            keys=unknown,
        )
    return settings


def is_admin(user_id: int) -> bool:
    """Return True when *user_id* is in the admin allowlist."""
    return user_id in get_settings().admin_id_list()
