"""Tier resolution.

Admin is a privilege flag rather than a purchasable tier (spec 2.1), so it is
handled here instead of appearing as a fourth row in the tier table. A grant that
has expired reverts the user to Free silently (spec 2.2).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

from app.config import Settings, TierLimits

TierName = Literal["free", "pro", "premium"]

TIER_NAMES: tuple[TierName, ...] = ("free", "pro", "premium")

ADMIN_LABEL = "👑 Admin"
TIER_LABELS: dict[TierName, str] = {
    "pro": "✨ Pro",
    "premium": "✨ Premium",
    "free": "👤 Free",
}


@dataclass(frozen=True)
class EffectiveTier:
    """What a user actually gets right now, after expiry and admin are applied."""

    name: TierName
    is_admin: bool
    limits: TierLimits
    priority: float

    @property
    def label(self) -> str:
        return ADMIN_LABEL if self.is_admin else TIER_LABELS[self.name]


def _as_aware(moment: datetime | None) -> datetime | None:
    """Treat a naive timestamp as UTC rather than rejecting it.

    Grants written before a timezone column existed, or by hand in SQL, would
    otherwise raise instead of simply expiring.
    """
    if moment is None:
        return None
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)


def unexpired_tier(
    tier: str,
    tier_expiry: datetime | None,
    *,
    now: datetime | None = None,
) -> TierName:
    """The stored tier, downgraded to ``free`` if its grant has expired."""
    if tier not in TIER_NAMES:
        return "free"
    expiry = _as_aware(tier_expiry)
    if expiry is None:
        return tier  # type: ignore[return-value]
    current = now or datetime.now(UTC)
    return tier if expiry > current else "free"  # type: ignore[return-value]


def resolve(
    settings: Settings,
    *,
    tier: str = "free",
    tier_expiry: datetime | None = None,
    is_admin: bool = False,
    now: datetime | None = None,
) -> EffectiveTier:
    """Resolve stored state into the limits that apply right now.

    An admin keeps the *premium* concurrency and size limits rather than a special
    tier, plus the highest queue priority (spec 2.1 and section 6).
    """
    if is_admin:
        return EffectiveTier(
            name="premium",
            is_admin=True,
            limits=settings.tier.premium,
            priority=settings.admin.priority,
        )

    name = unexpired_tier(tier, tier_expiry, now=now)
    return EffectiveTier(
        name=name,
        is_admin=False,
        limits=get_limits(settings, name),
        priority=get_limits(settings, name).priority,
    )


def get_limits(settings: Settings, tier: str) -> TierLimits:
    if tier not in TIER_NAMES:
        tier = "free"
    return getattr(settings.tier, tier)


def priority_for(settings: Settings, tier: str, *, is_admin: bool = False) -> float:
    """Lower sorts first. Admin is ahead of every tier."""
    return settings.admin.priority if is_admin else get_limits(settings, tier).priority


def is_admin(settings: Settings, user_id: int | None) -> bool:
    """Whether this user id is listed as an operator.

    A missing ``from_user`` (a channel post, or a service message) is not an admin,
    and an empty admin list never matches a real id — ``None`` is rejected explicitly
    so it cannot be mistaken for a member of the list.
    """
    return user_id is not None and user_id in settings.admin_ids


def format_limit_mb(link_total_mb: int) -> str:
    """Render a size limit the way the spec's copy expects, with ``∞`` for unlimited."""
    if link_total_mb <= 0:
        return "∞"
    if link_total_mb >= 1024:
        whole, remainder = divmod(link_total_mb, 1024)
        return f"{whole} GB" if remainder == 0 else f"{link_total_mb} MB"
    return f"{link_total_mb} MB"
