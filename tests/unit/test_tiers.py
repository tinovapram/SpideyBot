"""Tier resolution: expiry, admin, and the labels the UI prints."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.config import build_settings
from app.tiers import (
    ADMIN_LABEL,
    format_limit_mb,
    is_admin,
    priority_for,
    resolve,
    unexpired_tier,
)

NOW = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)


def _settings(fake_secrets: dict[str, str]):
    return build_settings(**fake_secrets)


# ── unexpired_tier ──────────────────────────────────────────────────────


def test_expired_grant_reverts_to_free() -> None:
    assert unexpired_tier("premium", NOW - timedelta(seconds=1), now=NOW) == "free"


def test_future_grant_is_kept() -> None:
    assert unexpired_tier("pro", NOW + timedelta(days=1), now=NOW) == "pro"


def test_grant_without_expiry_never_lapses() -> None:
    assert unexpired_tier("premium", None, now=NOW) == "premium"


def test_unknown_tier_name_is_free() -> None:
    assert unexpired_tier("platinum", None, now=NOW) == "free"


def test_naive_expiry_is_read_as_utc() -> None:
    """A hand-written SQL grant has no timezone; it must expire, not raise."""
    assert unexpired_tier("pro", datetime(2000, 1, 1), now=NOW) == "free"


# ── resolve ─────────────────────────────────────────────────────────────


def test_free_user_gets_free_limits(fake_secrets: dict[str, str]) -> None:
    settings = _settings(fake_secrets)
    tier = resolve(settings, tier="free", now=NOW)

    assert tier.name == "free"
    assert tier.limits == settings.tier.free
    assert tier.label == "👤 Free"
    assert not tier.is_admin


def test_expired_pro_resolves_to_free_limits(fake_secrets: dict[str, str]) -> None:
    settings = _settings(fake_secrets)
    tier = resolve(settings, tier="pro", tier_expiry=NOW - timedelta(days=1), now=NOW)

    assert tier.name == "free"
    assert tier.limits == settings.tier.free


def test_admin_gets_premium_limits_and_top_priority(fake_secrets: dict[str, str]) -> None:
    settings = _settings(fake_secrets)
    tier = resolve(settings, tier="free", is_admin=True, now=NOW)

    assert tier.is_admin
    assert tier.name == "premium"
    assert tier.limits == settings.tier.premium
    assert tier.priority == settings.admin.priority
    assert tier.label == ADMIN_LABEL
    # Admin must outrank every tier, not merely match the best one.
    assert tier.priority < settings.tier.premium.priority


def test_priority_orders_free_behind_paid(fake_secrets: dict[str, str]) -> None:
    settings = _settings(fake_secrets)

    assert (
        priority_for(settings, "free")
        > priority_for(settings, "pro")
        > priority_for(settings, "premium")
    )


# ── is_admin ────────────────────────────────────────────────────────────


def test_missing_user_is_never_admin(fake_secrets: dict[str, str]) -> None:
    settings = _settings(fake_secrets).model_copy(update={"admin_ids": [123]})

    assert not is_admin(settings, None)
    assert is_admin(settings, 123)
    assert not is_admin(settings, 456)


def test_empty_admin_list_matches_nobody(fake_secrets: dict[str, str]) -> None:
    settings = _settings(fake_secrets)

    assert settings.admin_ids == []
    assert not is_admin(settings, None)
    assert not is_admin(settings, 0)


# ── format_limit_mb ─────────────────────────────────────────────────────


def test_unlimited_renders_as_infinity() -> None:
    assert format_limit_mb(0) == "∞"
    assert format_limit_mb(-1) == "∞"


def test_whole_gigabytes_render_as_gigabytes() -> None:
    assert format_limit_mb(1024) == "1 GB"
    assert format_limit_mb(10240) == "10 GB"


def test_awkward_sizes_stay_in_megabytes() -> None:
    """1536 MB is not 1.5 GB — the spec's copy only has whole units."""
    assert format_limit_mb(1536) == "1536 MB"
    assert format_limit_mb(500) == "500 MB"
