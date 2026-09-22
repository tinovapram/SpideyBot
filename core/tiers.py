"""Tier and quota policy definitions — the single source of truth.

Tiers are data, not booleans. Every quota dimension a download path needs is
derived from :func:`tier_policy`. Admin is a bypass flag, not a tier.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum

_MB = 1024 * 1024
_GB = 1024 * _MB
_TB = 1024 * _GB


class SitePolicyMode(str, Enum):
    """How a tier's site gating is interpreted."""

    ALLOWLIST = "allowlist"
    ALL = "all"


@dataclass(frozen=True)
class SitePolicy:
    """Which sites a tier may download from."""

    mode: SitePolicyMode = SitePolicyMode.ALL
    allow: frozenset[str] = frozenset()
    deny: frozenset[str] = frozenset()

    def allows(self, site: str) -> bool:
        if site in self.deny:
            return False
        if self.mode is SitePolicyMode.ALL:
            return True
        return site in self.allow


@dataclass(frozen=True)
class QuotaPolicy:
    """All quota dimensions for one tier. ``None`` means unlimited."""

    size_limit_bytes: int                  # per-file cap
    link_total_limit_bytes: int | None     # per-link total (all files combined)
    daily_downloads: int | None            # downloads per day
    daily_bytes: int | None                # bandwidth per day
    monthly_bytes: int | None              # bandwidth per month
    concurrent: int                        # simultaneous downloads per user
    priority: float                        # queue priority (lower = sooner)
    history_retention_days: int            # /history look-back window
    sites: SitePolicy


# ── Canonical site names (must match downloader registry keys) ────

SOCIAL_SITES = frozenset({
    "youtube", "twitter", "tiktok", "reddit", "instagram", "soundcloud",
    "bluesky", "threads", "pinterest", "douyin", "kuaishou", "dailymotion",
    "tumblr", "snapchat", "linkedin", "capcut", "spotify",
})

VIDEO_HOST_SITES = frozenset({
    "doodstream", "streamtape", "mixdrop", "streamwish", "luluvdoo",
    "bysejikuar", "vidara", "telegram", "mega", "cyberdropdl",
})

HEAVY_SITES = frozenset({"terabox"})

ALL_SITES = SOCIAL_SITES | VIDEO_HOST_SITES | HEAVY_SITES

# Shared deny list — applies to every tier (empty by default).
DENY_ALL = frozenset()


TIERS: dict[str, QuotaPolicy] = {
    "free": QuotaPolicy(
        size_limit_bytes=100 * _MB,
        link_total_limit_bytes=500 * _MB,
        daily_downloads=10,
        daily_bytes=1 * _GB,
        monthly_bytes=20 * _GB,
        concurrent=1,
        priority=2.0,
        history_retention_days=7,
        sites=SitePolicy(mode=SitePolicyMode.ALLOWLIST, allow=SOCIAL_SITES, deny=DENY_ALL),
    ),
    "pro": QuotaPolicy(
        size_limit_bytes=2 * _GB,
        link_total_limit_bytes=10 * _GB,
        daily_downloads=50,
        daily_bytes=25 * _GB,
        monthly_bytes=500 * _GB,
        concurrent=3,
        priority=1.0,
        history_retention_days=30,
        sites=SitePolicy(
            mode=SitePolicyMode.ALLOWLIST,
            allow=SOCIAL_SITES | VIDEO_HOST_SITES,
            deny=DENY_ALL,
        ),
    ),
    "premium": QuotaPolicy(
        size_limit_bytes=10 * _GB,
        link_total_limit_bytes=None,
        daily_downloads=200,
        daily_bytes=100 * _GB,
        monthly_bytes=2 * _TB,
        concurrent=8,
        priority=0.5,
        history_retention_days=90,
        sites=SitePolicy(mode=SitePolicyMode.ALL, deny=DENY_ALL),
    ),
}

# Admin runs ahead of every tier.
PRIORITY_ADMIN = 0.2
DEFAULT_TIER = "free"


def tier_policy(tier: str) -> QuotaPolicy:
    """Return the quota policy for *tier*, defaulting to ``free``."""
    return TIERS.get(tier, TIERS[DEFAULT_TIER])


def is_known_tier(tier: str) -> bool:
    """Return True when *tier* is a configured tier name."""
    return tier in TIERS

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
    """Return ``(size_limit_bytes, human label)`` for a download task.

    Admin bypasses the per-file cap entirely.
    """
    if is_admin:
        return float("inf"), "unlimited"
    limit = tier_policy(tier).size_limit_bytes
    if limit % _GB == 0:
        label = f"{limit // _GB} GB"
    else:
        label = f"{limit // _MB} MB"
    return limit, label
