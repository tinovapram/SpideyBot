"""The pure helpers behind `/start`, which need no database.

The handler itself is covered by the acceptance layer, because it registers users and this
file is for the parts that are worth testing without infrastructure.
"""

from __future__ import annotations

import pytest

from app.referral import parse_referrer_id
from handlers.start import referral_payload, upgrade_keyboard
from tests.fakes.messages import FakeMessage, FakeUser

# ── the Upgrade button (AC-1, plan D29) ─────────────────────────────────


def test_upgrade_keyboard_is_absent_without_a_url() -> None:
    """No destination appears anywhere in the spec, so nothing is invented."""
    assert upgrade_keyboard("") is None


def test_upgrade_keyboard_builds_a_labelled_url_button() -> None:
    markup = upgrade_keyboard("https://example.invalid/upgrade")

    assert markup is not None
    button = markup.inline_keyboard[0][0]
    assert button.url == "https://example.invalid/upgrade"
    assert button.text


# ── the referral payload ────────────────────────────────────────────────


def test_payload_is_read_from_the_split_command() -> None:
    message = FakeMessage(FakeUser(1), command=("start", "4242"))

    assert referral_payload(message) == "4242"


def test_an_absent_payload_is_none() -> None:
    assert referral_payload(FakeMessage(FakeUser(1))) is None


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ("4242", 4242),
        ("  4242  ", 4242),
        (None, None),
        ("", None),
        ("abc", None),  # non-numeric is ignored without error (FR-1.2)
        ("0", None),  # not a usable Telegram id
        ("-5", None),
        ("4.2", None),
    ],
)
def test_unusable_payloads_are_ignored(payload: str | None, expected: int | None) -> None:
    assert parse_referrer_id(payload) == expected
