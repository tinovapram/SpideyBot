"""Tier and quota policy definitions -- the single source of truth.

Tiers are data, not booleans.  Every quota dimension a download path needs is
derived from :func:`tier_policy`.  Admin is a bypass flag, not a tier.
Tier parameters are loaded from env vars at first access via :func:`_load_tiers`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

_MB = 1024 * 1024
_GB = 1024 * _MB

@dataclass(frozen=True)
class QuotaPolicy:
    """All quota dimensions for one tier. ``None`` means unlimited."""

    link_total_limit_bytes: int | None     # total size limit per link (all files combined)
    daily_downloads: int | None            # downloads per day
    daily_bytes: int | None                # bandwidth per day
    concurrent: int                        # simultaneous downloads per user
    priority: float                        # queue priority (lower = sooner)

# -- Canonical site names (must match downloader registry keys) -----

HEAVY_SITES = frozenset({"terabox"})

# Admin runs ahead of every tier.
PRIORITY_ADMIN = 0.2
DEFAULT_TIER = "free"

def _load_tiers() -> dict[str, QuotaPolicy]:
    """Build tier dict from env vars. Falls back to built-in defaults."""
    from core.config import get_settings
    s = get_settings()
    return {
        "free": QuotaPolicy(
            link_total_limit_bytes=s.tier_free_link_total_mb * _MB,
            daily_downloads=s.tier_free_daily_downloads,
            daily_bytes=s.tier_free_daily_mb * _MB,
            concurrent=s.tier_free_concurrent,
            priority=s.tier_free_priority,
        ),
        "pro": QuotaPolicy(
            link_total_limit_bytes=s.tier_pro_link_total_mb * _MB,
            daily_downloads=s.tier_pro_daily_downloads,
            daily_bytes=s.tier_pro_daily_mb * _MB,
            concurrent=s.tier_pro_concurrent,
            priority=s.tier_pro_priority,
        ),
        "premium": QuotaPolicy(
            link_total_limit_bytes=None,
            daily_downloads=s.tier_premium_daily_downloads,
            daily_bytes=s.tier_premium_daily_mb * _MB,
            concurrent=s.tier_premium_concurrent,
            priority=s.tier_premium_priority,
        ),
    }

TIERS: dict[str, QuotaPolicy] | None = None

def _ensure_tiers() -> dict[str, QuotaPolicy]:
    global TIERS  # noqa: PLW0603
    if TIERS is None:
        TIERS = _load_tiers()
    return TIERS

def tier_policy(tier: str) -> QuotaPolicy:
    """Return the quota policy for *tier*, defaulting to ``free``."""
    return _ensure_tiers().get(tier, _ensure_tiers()[DEFAULT_TIER])

def is_known_tier(tier: str) -> bool:
    """Return True when *tier* is a configured tier name."""
    return tier in _ensure_tiers()

def effective_tier(tier: str, tier_expiry: datetime | None, *, is_admin: bool = False) -> str:
    """Return the user's effective tier, accounting for expiry and admin status.

    Admins always get ``"premium"`` regardless of stored tier.
    If *tier_expiry* is in the past, returns ``DEFAULT_TIER`` ("free").
    Unknown tiers also fall back to ``DEFAULT_TIER``.
    """
    if is_admin:
        return "premium"
    if tier_expiry is not None:
        expiry = tier_expiry if tier_expiry.tzinfo else tier_expiry.replace(tzinfo=timezone.utc)
        if expiry < datetime.now(timezone.utc):
            return DEFAULT_TIER
    return tier if is_known_tier(tier) else DEFAULT_TIER

def size_limit(tier: str, is_admin: bool = False) -> tuple[int | float, str]:
    """Return ``(link_total_limit_bytes, human label)`` for a download task.

    Admin bypasses the cap entirely.  ``None`` in the policy means unlimited.
    """
    if is_admin:
        return float("inf"), "unlimited"
    limit = tier_policy(tier).link_total_limit_bytes
    if limit is None:
        return float("inf"), "unlimited"
    if limit % _GB == 0:
        label = f"{limit // _GB} GB"
    else:
        label = f"{limit // _MB} MB"
    return limit, label
