"""Configuration layer: precedence, validation, and fail-loudly behaviour."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from app.config import (
    Settings,
    TomlFilesSource,
    _deep_merge,
    _unknown_keys,
    build_settings,
)


def make_settings(**overrides: Any) -> Settings:
    secrets = {
        "tg_api_id": "1",
        "tg_api_hash": "a" * 32,
        "tg_bot_token": "1:fake",
        "db_password": "pw",
    }
    secrets.update(overrides)
    return build_settings(**secrets)


# ── loading ─────────────────────────────────────────────────────────────


def test_default_toml_is_loaded(fake_secrets: dict[str, str]) -> None:
    settings = build_settings(**fake_secrets)

    # Values come from config/default.toml, not from code defaults.
    assert settings.tier.free.daily_downloads == 10
    assert settings.tier.free.link_total_mb == 500
    assert settings.tier.pro.concurrent == 2
    assert settings.tier.premium.link_total_mb == 0  # 0 means unlimited
    assert settings.admin.priority < settings.tier.premium.priority
    assert settings.ux.album_size == 10
    assert settings.db.port == 55432  # non-default, so a real Postgres can coexist


def test_missing_secrets_fail_startup() -> None:
    with pytest.raises(ValidationError) as excinfo:
        Settings()

    message = str(excinfo.value)
    assert "tg_api_id" in message
    assert "tg_api_hash" in message
    assert "tg_bot_token" in message


def test_db_password_is_optional(fake_secrets: dict[str, str]) -> None:
    """The bundled cluster generates its own password and stores it under ``data/``."""
    secrets = {key: value for key, value in fake_secrets.items() if key != "db_password"}

    settings = build_settings(**secrets)

    assert settings.db_password is None


def test_upgrade_url_is_optional(fake_secrets: dict[str, str]) -> None:
    """AC-1 wants the button, but the spec names no destination, so empty is valid."""
    assert build_settings(**fake_secrets).ux.upgrade_url == ""


def test_upgrade_url_must_be_a_link_telegram_accepts(
    fake_secrets: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SPIDEY_UX__UPGRADE_URL", "example.com/upgrade")

    with pytest.raises(ValidationError, match="upgrade_url"):
        build_settings(**fake_secrets)


@pytest.mark.parametrize("url", ["https://example.com/upgrade", "tg://resolve?domain=spideybot"])
def test_upgrade_url_accepts_links(
    fake_secrets: dict[str, str], monkeypatch: pytest.MonkeyPatch, url: str
) -> None:
    monkeypatch.setenv("SPIDEY_UX__UPGRADE_URL", url)

    assert build_settings(**fake_secrets).ux.upgrade_url == url


def test_secrets_are_not_leaked_by_repr(fake_secrets: dict[str, str]) -> None:
    settings = build_settings(**fake_secrets)
    password = fake_secrets["db_password"]

    assert settings.db_password.get_secret_value() == password
    assert password not in repr(settings)
    assert password not in str(settings)


# ── fail loudly on typos ────────────────────────────────────────────────


def test_unknown_toml_key_is_rejected(tmp_path: Path) -> None:
    bad = tmp_path / "default.toml"
    bad.write_text("[ux]\nalbum_size = 5\nrender_intervall = 1.0\n", encoding="utf-8")

    source = TomlFilesSource(Settings, (bad,))
    with pytest.raises(ValueError, match="unknown key"):
        source()


def test_unknown_toml_section_is_rejected(tmp_path: Path) -> None:
    bad = tmp_path / "default.toml"
    bad.write_text("[tiers.free]\ndaily_downloads = 5\n", encoding="utf-8")

    source = TomlFilesSource(Settings, (bad,))
    with pytest.raises(ValueError, match="tiers"):
        source()


def test_unknown_keys_walks_nested_sections() -> None:
    data = {"tier": {"free": {"link_total_mb": 1, "nope": 2}}}
    assert _unknown_keys(data, Settings) == ["tier.free.nope"]


def test_missing_toml_files_are_skipped() -> None:
    source = TomlFilesSource(Settings, (Path("does-not-exist.toml"),))
    assert source() == {}


def test_deep_merge_lets_later_files_win() -> None:
    base = {"tier": {"free": {"daily_downloads": 10, "concurrent": 1}}, "ux": {"album_size": 10}}
    override = {"tier": {"free": {"daily_downloads": 20}}}

    merged = _deep_merge(base, override)

    assert merged["tier"]["free"] == {"daily_downloads": 20, "concurrent": 1}
    assert merged["ux"]["album_size"] == 10
    # inputs untouched
    assert base["tier"]["free"]["daily_downloads"] == 10


# ── precedence ──────────────────────────────────────────────────────────


def test_env_overrides_toml(fake_secrets: dict[str, str], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SPIDEY_TIER__FREE__DAILY_DOWNLOADS", "99")

    settings = build_settings(**fake_secrets)

    assert settings.tier.free.daily_downloads == 99
    assert settings.tier.free.concurrent == 1  # untouched keys keep their TOML value


def test_init_kwargs_override_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SPIDEY_DB_PASSWORD", "from-env")

    settings = build_settings(
        tg_api_id="1",
        tg_api_hash="a" * 32,
        tg_bot_token="1:fake",
        db_password="from-init",
    )

    assert settings.db_password.get_secret_value() == "from-init"


def test_blank_env_value_counts_as_unset(
    fake_secrets: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    # .env.example ships every key present-but-blank; blank must not read as a value.
    monkeypatch.setenv("SPIDEY_TEST_PHONE", "")
    monkeypatch.setenv("SPIDEY_GDL_COOKIES_FROM_BROWSER", "")

    settings = build_settings(**fake_secrets)

    assert settings.test_phone is None
    assert settings.gdl_cookies_from_browser is None


# ── validation ──────────────────────────────────────────────────────────


def test_album_size_above_telegram_limit_is_rejected(
    fake_secrets: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    # Telegram media groups hold at most 10 items, so >10 is undeliverable.
    monkeypatch.setenv("SPIDEY_UX__ALBUM_SIZE", "11")

    with pytest.raises(ValidationError, match="album_size"):
        build_settings(**fake_secrets)


def test_magic_word_must_be_a_single_token(
    fake_secrets: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SPIDEY_UX__DEFAULT_MAGIC_WORD", "two words")

    with pytest.raises(ValidationError, match="single token"):
        build_settings(**fake_secrets)


def test_custom_client_profile_requires_its_section(
    fake_secrets: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SPIDEY_CLIENT__PROFILE", "custom")

    with pytest.raises(ValidationError, match=r"\[client.custom\]"):
        build_settings(**fake_secrets)


def test_client_profile_selects_the_active_identity(fake_secrets: dict[str, str]) -> None:
    settings = build_settings(**fake_secrets)

    assert settings.client.profile == "desktop"
    assert settings.client.active().device_model == "Desktop"
    assert settings.client.active().lang_code == "en"


def test_admin_ids_accept_commas(
    fake_secrets: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SPIDEY_ADMIN_IDS", "123456789, 987654321")

    settings = build_settings(**fake_secrets)

    assert settings.admin_ids == [123456789, 987654321]


# ── derived helpers ─────────────────────────────────────────────────────


def test_database_url_quotes_password_and_safe_variant_masks_it(
    fake_secrets: dict[str, str],
) -> None:
    settings = build_settings(**{**fake_secrets, "db_password": "p@ss word"})

    assert "%40" in settings.database_url and "%20" in settings.database_url
    # A space must not become '+': userinfo is not a query string, and a driver
    # would read '+' as a literal plus in the password.
    credentials = settings.database_url.split("//", 1)[1].split("@", 1)[0]
    assert "+" not in credentials
    assert settings.database_url_safe.endswith("@127.0.0.1:55432/spideybot")
    assert "p@ss" not in settings.database_url_safe
    assert "***" in settings.database_url_safe


def test_terabox_accounts_split_on_pipe_not_semicolon(
    fake_secrets: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    # ';' separates pairs *within* one cookie, so it cannot separate accounts.
    monkeypatch.setenv(
        "SPIDEY_TERABOX_COOKIES", "ndus=a; lang=en|ndus=b; lang=fr"
    )

    settings = build_settings(**fake_secrets)

    assert settings.terabox_cookie_accounts() == ["ndus=a; lang=en", "ndus=b; lang=fr"]


def test_secret_values_includes_each_cookie_account(
    fake_secrets: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SPIDEY_TERABOX_COOKIES", "ndus=aaaa|ndus=bbbb")

    values = build_settings(**fake_secrets).secret_values()

    assert "ndus=aaaa" in values
    assert "ndus=bbbb" in values
    # The joined form is registered as well. It is a superset, so redaction cannot
    # miss an account merely because only the combined string was logged.
    assert "ndus=aaaa|ndus=bbbb" in values
