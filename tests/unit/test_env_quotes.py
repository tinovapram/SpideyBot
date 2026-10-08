"""Quoting in `.env` must behave identically on a host and in a container (plan R14).

Found by running the bot in Docker: ``python-dotenv`` strips surrounding quotes while
Docker's ``--env-file`` does not, so a value quoted the way `.env.example` recommends
loaded correctly locally and failed validation in the container:

    Value error, invalid literal for int() with base 10: '"72363538'

The tests below drive both paths with the *same* value, because the point is that one
file works in both places rather than that each place can be made to work.
"""

from __future__ import annotations

import os

import pytest
from pydantic import ValidationError

from app import config, paths
from app.config import build_settings, normalise_env_quotes, strip_env_quotes

# What the container received, quotes and all.
QUOTED_ADMIN_IDS = '"72363538,8906762917"'
EXPECTED_ADMIN_IDS = [72363538, 8906762917]

# Field names, for build_settings(**...), which outranks every other source.
SECRET_FIELDS = {
    "tg_api_id": "123456",
    "tg_api_hash": "a" * 32,
    "tg_bot_token": "123:fake",
}


@pytest.fixture
def secrets_in_env(monkeypatch):
    """Secrets supplied the way a container does: straight into the environment."""
    for name, value in SECRET_FIELDS.items():
        monkeypatch.setenv(f"SPIDEY_{name.upper()}", value)


@pytest.fixture
def isolated_environ():
    """Restore the whole environment afterwards.

    ``load_dotenv`` writes into ``os.environ`` directly, so monkeypatch has no record of
    the keys it added and cannot undo them.
    """
    snapshot = dict(os.environ)
    try:
        yield
    finally:
        os.environ.clear()
        os.environ.update(snapshot)


@pytest.fixture
def no_env_file(tmp_path, monkeypatch):
    """Point at a file that does not exist, which is the container's situation."""
    monkeypatch.setattr(paths, "ENV_FILE", tmp_path / "absent.env")


# ── strip_env_quotes ────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("plain", "plain"),
        ('"quoted"', "quoted"),
        ("'quoted'", "quoted"),
        ('"1,2"', "1,2"),
        ("''", ""),
        ('"', '"'),
        ("", ""),
        # Only a matching pair wrapping the whole value counts.
        ("\"mismatched'", "\"mismatched'"),
        ("it's fine", "it's fine"),
        ('he said "hi"', 'he said "hi"'),
        # The interior is never interpreted, so spacing survives.
        ('"  spaced  "', "  spaced  "),
    ],
)
def test_strip_env_quotes(raw: str, expected: str) -> None:
    assert strip_env_quotes(raw) == expected


# ── normalise_env_quotes ────────────────────────────────────────────────


def test_only_prefixed_values_are_touched(monkeypatch) -> None:
    monkeypatch.setenv("SPIDEY_ADMIN_IDS", QUOTED_ADMIN_IDS)
    monkeypatch.setenv("SOME_OTHER_TOOL", '"leave me alone"')

    changed = normalise_env_quotes()

    assert changed == 1
    assert os.environ["SPIDEY_ADMIN_IDS"] == "72363538,8906762917"
    assert os.environ["SOME_OTHER_TOOL"] == '"leave me alone"'


def test_unquoted_values_are_not_reported_as_changed(monkeypatch) -> None:
    monkeypatch.setenv("SPIDEY_ADMIN_IDS", "1,2")

    assert normalise_env_quotes() == 0


def test_normalisation_is_idempotent(monkeypatch) -> None:
    monkeypatch.setenv("SPIDEY_ADMIN_IDS", QUOTED_ADMIN_IDS)

    normalise_env_quotes()

    assert normalise_env_quotes() == 0


# ── the container path ──────────────────────────────────────────────────


def test_quoted_value_from_docker_is_accepted(no_env_file, monkeypatch) -> None:
    """The R14 regression: Docker passes the quotes through, validation rejects them."""
    monkeypatch.setenv("SPIDEY_ADMIN_IDS", QUOTED_ADMIN_IDS)

    assert config.load_env_file() is False  # no file: values arrived from Docker
    settings = build_settings(**SECRET_FIELDS)

    assert settings.admin_ids == EXPECTED_ADMIN_IDS


def test_the_container_path_is_what_actually_breaks_without_normalising(
    no_env_file, monkeypatch
) -> None:
    """Without the fix the value reaches the validator still quoted.

    Asserting on the failure keeps the test honest: if a future change made the value
    parse by accident, this documents what the original defect looked like.
    """
    monkeypatch.setenv("SPIDEY_ADMIN_IDS", QUOTED_ADMIN_IDS)

    with pytest.raises(ValidationError, match="invalid literal for int"):
        build_settings(**SECRET_FIELDS)


# ── the host path, with the same value ──────────────────────────────────


def test_the_same_quoted_file_loads_on_the_host(
    tmp_path, monkeypatch, secrets_in_env, isolated_environ
) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(f"SPIDEY_ADMIN_IDS={QUOTED_ADMIN_IDS}\n", encoding="utf-8")
    monkeypatch.setattr(paths, "ENV_FILE", env_file)

    assert config.load_env_file() is True
    settings = build_settings()

    assert settings.admin_ids == EXPECTED_ADMIN_IDS


# ── secrets travel the same route ───────────────────────────────────────


def test_quoted_secrets_are_unquoted_too(no_env_file, monkeypatch) -> None:
    """A password is the most likely value to need quoting, since it can contain '#'."""
    monkeypatch.setenv("SPIDEY_TG_API_ID", "123456")
    monkeypatch.setenv("SPIDEY_TG_API_HASH", f'"{"b" * 32}"')
    monkeypatch.setenv("SPIDEY_TG_BOT_TOKEN", "'123:fake'")
    monkeypatch.setenv("SPIDEY_DB_PASSWORD", '"p@ss w;ord#!"')

    config.load_env_file()
    settings = build_settings()

    assert settings.tg_api_hash.get_secret_value() == "b" * 32
    assert settings.tg_bot_token.get_secret_value() == "123:fake"
    assert settings.db_password.get_secret_value() == "p@ss w;ord#!"


def test_quoting_a_nested_setting_works(no_env_file, monkeypatch) -> None:
    """Nested overrides take the same path, so they must not be a separate case."""
    monkeypatch.setenv("SPIDEY_UX__ALBUM_SIZE", '"7"')

    config.load_env_file()
    settings = build_settings(**SECRET_FIELDS)

    assert settings.ux.album_size == 7
