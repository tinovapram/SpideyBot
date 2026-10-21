from __future__ import annotations

import pytest

from core import config


def test_unknown_spidey_keys_are_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "_env_file_keys", lambda: {"SPIDEY_FROM_ENV_FILE"})
    monkeypatch.setenv("SPIDEY_TYPO", "x")
    monkeypatch.setenv("SPIDEY_TG_API_ID", "1")

    keys = config.unknown_env_keys()

    assert "SPIDEY_TYPO" in keys
    assert "SPIDEY_FROM_ENV_FILE" in keys
    assert "SPIDEY_TG_API_ID" not in keys


def test_known_settings_are_derived_from_the_model() -> None:
    known = config._known_env_keys()

    assert "SPIDEY_DATABASE_URL" in known
    assert "SPIDEY_TG_BOT_TOKEN" in known
    assert "SPIDEY_CAMOUFOX_MAX_CONCURRENCY" in known
