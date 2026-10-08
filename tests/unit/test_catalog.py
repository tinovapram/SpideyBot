"""The message catalog: spec section 7 copy, and the placeholders inside it."""

from __future__ import annotations

import pytest

from app import catalog
from app.config import build_settings
from app.tiers import resolve


@pytest.fixture
def settings(fake_secrets: dict[str, str]):
    return build_settings(**fake_secrets)


# ── session badge (spec 2.2 / FR-13) ────────────────────────────────────


def test_no_session_file_shows_the_red_badge() -> None:
    assert catalog.session_badge(has_session=False, is_live=False) == "🔴 No session"


def test_stored_session_shows_the_saved_badge() -> None:
    badge = catalog.session_badge(has_session=True, is_live=False)

    assert badge == "🟡 Session saved (not running)"


def test_live_client_wins_over_the_stored_file() -> None:
    assert catalog.session_badge(has_session=True, is_live=True) == "🟢 Connected"


def test_live_client_without_a_file_is_still_connected() -> None:
    """Sessions can be in-memory (a session string), so the file is not authoritative."""
    assert catalog.session_badge(has_session=False, is_live=True) == "🟢 Connected"


# ── help ────────────────────────────────────────────────────────────────


def test_help_lists_every_documented_command() -> None:
    text = catalog.help_message()

    for command in (
        "/start",
        "/help",
        "/dl",
        "/dt",
        "/cancel",
        "/status",
        "/account",
        "/quota",
        "/sites",
        "/bypass",
        "/referral",
    ):
        assert command in text


def test_admin_block_is_absent_for_normal_users() -> None:
    text = catalog.help_message(is_admin=False)

    assert "Admin only" not in text
    assert "/stats" not in text


def test_admin_block_appears_for_admins() -> None:
    text = catalog.help_message(is_admin=True)

    assert "**Admin only**" in text
    assert "/addpremium" in text


def test_magic_word_is_interpolated() -> None:
    assert "`WOW2`" in catalog.help_message(magic_word="WOW2")


# ── welcome ─────────────────────────────────────────────────────────────


def test_welcome_composes_the_specs_blocks_in_order(settings) -> None:
    tier = resolve(settings)
    text = catalog.welcome_message("Ada", tier, magic_word="WOW")

    assert catalog.WELCOME in text
    assert "**Essential commands**" in text
    assert "**Notes**" in text
    assert text.endswith("**Welcome, Ada!**")
    assert text.index(catalog.WELCOME) < text.index("**Essential commands**")


def test_free_tier_reminder_is_shown_only_to_free_users(settings) -> None:
    free = catalog.welcome_message("Ada", resolve(settings, tier="free"))
    pro = catalog.welcome_message("Ada", resolve(settings, tier="pro"))

    assert catalog.FREE_TIER_NOTE in free
    assert catalog.FREE_TIER_NOTE not in pro


def test_admin_welcome_shows_admin_commands_and_no_free_reminder(settings) -> None:
    tier = resolve(settings, is_admin=True)
    text = catalog.welcome_message("Ada", tier)

    assert "**Admin only**" in text
    assert catalog.FREE_TIER_NOTE not in text


def test_welcome_reports_the_configured_limit(settings) -> None:
    text = catalog.welcome_message("Ada", resolve(settings, tier="free"))

    assert "Link limit: 500 MB" in text


def test_welcome_reports_the_session_state(settings) -> None:
    text = catalog.welcome_message("Ada", resolve(settings), has_session=True, is_live=True)

    assert f"Sessions: {catalog.CONNECTED}" in text


@pytest.mark.parametrize(
    "build",
    [
        lambda s: catalog.help_message(),
        lambda s: catalog.help_message(is_admin=True),
        lambda s: catalog.welcome_message("Ada", resolve(s)),
        lambda s: catalog.welcome_message("Ada", resolve(s, tier="pro")),
    ],
)
def test_no_placeholder_survives_rendering(settings, build) -> None:
    """A leftover brace means a value was never substituted and the user sees `{tier_label}`."""
    assert "{" not in build(settings)


# ── usage texts (spec 7.2) ──────────────────────────────────────────────


def test_usage_texts_name_their_command() -> None:
    assert "**Usage:** /dl *URL*" in catalog.USAGE_DL
    assert "**Usage:** /dt *Telegram link*" in catalog.USAGE_DT
    assert "**Usage:** /cancel *ID*" in catalog.USAGE_CANCEL


def test_usage_cancel_points_at_status() -> None:
    assert "/status" in catalog.USAGE_CANCEL


def test_usage_dt_mentions_the_session_requirement() -> None:
    assert "/login" in catalog.USAGE_DT
