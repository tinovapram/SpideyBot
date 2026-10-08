"""Shared test fixtures.

Living at the project root also places the repository root on ``sys.path``, so
tests can ``import app`` without an installed package or a src layout.
"""

from __future__ import annotations

import pytest

FAKE_SECRETS: dict[str, str] = {
    "tg_api_id": "123456",
    "tg_api_hash": "0" * 32,
    "tg_bot_token": "123456:FAKE-TOKEN",
    "db_password": "fake-password",
}


@pytest.fixture
def fake_secrets() -> dict[str, str]:
    """Minimal valid secrets.

    Tests pass these explicitly rather than relying on a developer's real ``.env``,
    so the suite is deterministic and never touches real credentials.
    """
    return dict(FAKE_SECRETS)
