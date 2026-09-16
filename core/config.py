"""Application configuration (typed, pydantic-settings).

Every setting uses the ``SPIDEY_`` env prefix and is loaded once via the
cached :func:`get_settings`. Validation happens at first access, so a broken
``.env`` fails fast at startup.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from utils import paths


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
    database_url: str = "postgresql+asyncpg://spidey:spidey@localhost:5432/spideybot"

    # ── Security ─────────────────────────────────────────────────
    session_encrypt_key: str = ""

    # ── TeraBox ──────────────────────────────────────────────────
    terabox_cookies: str = ""  # single or multi-account, "|"-delimited
    terabox_jstoken: str = ""
    terabox_bdstoken: str = ""
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

    # ── Queue / jobs ─────────────────────────────────────────────
    job_lease_seconds: int = 300
    job_max_attempts: int = 3

    # ── Admin ────────────────────────────────────────────────────
    admin_ids: list[int] = []

    # ── Rate limiting ────────────────────────────────────────────
    rate_limit_per_minute: int = 12

    # ── User sessions (on-demand lifecycle) ──────────────────────
    session_max_live: int = 50
    session_idle_seconds: int = 600

    # ── Retention ────────────────────────────────────────────────
    downloads_retention_days: int = 3

    # ── Referral ─────────────────────────────────────────────────
    referral_daily_bonus: int = 10
    referral_bonus_days: int = 30

    @field_validator("admin_ids", mode="before")
    @classmethod
    def _parse_admin_ids(cls, value):
        if value is None or value == "":
            return []
        if isinstance(value, str):
            return [int(x) for x in value.split(",") if x.strip().isdigit()]
        return value

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


@lru_cache
def get_settings() -> Settings:
    """Return the cached settings instance."""
    return Settings()


def is_admin(user_id: int) -> bool:
    """Return True when *user_id* is in the admin allowlist."""
    return user_id in get_settings().admin_ids
