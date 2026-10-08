"""Kurigram wiring: client identity, and how the bot client is assembled."""

from __future__ import annotations

import asyncio

import pytest
from pyrogram import enums
from pyrogram.handlers import MessageHandler

from app import paths
from app.config import build_settings
from telegram import clients
from telegram.profiles import identity_kwargs


@pytest.fixture
def settings(fake_secrets: dict[str, str]):
    return build_settings(**fake_secrets)


# ── identity (plan D16) ─────────────────────────────────────────────────


def test_identity_comes_from_the_profile(settings) -> None:
    profile = settings.client.active()
    kwargs = identity_kwargs(profile)

    assert kwargs["device_model"] == profile.device_model
    assert kwargs["system_version"] == profile.system_version
    assert kwargs["app_version"] == profile.app_version
    assert kwargs["lang_code"] == profile.lang_code
    assert kwargs["lang_pack"] == profile.lang_pack


def test_desktop_is_the_default_profile(settings) -> None:
    assert settings.client.profile == "desktop"
    assert settings.client.active() is settings.client.desktop


def test_switching_profiles_switches_the_strings(settings) -> None:
    switched = settings.model_copy(deep=True)
    switched.client.profile = "android"

    assert switched.client.active() is switched.client.android
    assert (
        identity_kwargs(switched.client.active())["device_model"]
        == switched.client.android.device_model
    )


def test_platform_is_mapped_to_the_enum_kurigram_expects(settings) -> None:
    platform = identity_kwargs(settings.client.desktop).get("client_platform")

    assert platform is not None
    assert platform.name == "DESKTOP"


def test_unknown_platform_is_dropped_rather_than_raising(settings) -> None:
    """A cosmetic field must never stop the bot from starting."""
    profile = settings.client.desktop.model_copy(update={"platform": "NOT_A_PLATFORM"})

    assert "client_platform" not in identity_kwargs(profile)


# ── the bot client ──────────────────────────────────────────────────────


@pytest.fixture
def bot_client(settings, tmp_path, monkeypatch):
    """The real bot client, with its session file kept out of ``data/``."""
    monkeypatch.setattr(paths, "BOT_DIR", tmp_path)
    return clients.build_bot(settings)


def test_bot_parses_markdown_so_the_catalog_renders(bot_client) -> None:
    """The copy relies on `**bold**` and `*italic*`; the library default is neither."""
    assert bot_client.parse_mode is enums.ParseMode.MARKDOWN


def test_bot_loads_handlers_from_the_handlers_package(bot_client) -> None:
    assert bot_client.plugins == {"root": "handlers"}


def test_bot_receives_credentials_and_identity(bot_client, settings) -> None:
    assert bot_client.device_model == settings.client.desktop.device_model
    assert bot_client.api_id == int(settings.tg_api_id.get_secret_value())
    assert bot_client.api_hash == settings.tg_api_hash.get_secret_value()


def test_constructing_the_bot_does_not_connect(bot_client) -> None:
    """Configuration must stay separable from I/O, or nothing is unit-testable."""
    assert not bot_client.is_connected


async def test_both_commands_register_as_message_handlers(settings, tmp_path, monkeypatch) -> None:
    """Kurigram loads plugins from ``start()``, so drive the loader directly and stay offline.

    Handlers are added through ``loop.create_task``, so they appear a tick later — which
    is also why the client is built inside this coroutine: Kurigram captures whichever
    loop is current, and a loop that never runs would silently register nothing.
    """
    monkeypatch.setattr(paths, "BOT_DIR", tmp_path)
    client = clients.build_bot(settings)

    client.load_plugins()
    await asyncio.sleep(0.05)

    handlers = [h for group in client.dispatcher.groups.values() for h in group]
    registered = {getattr(handler.callback, "__name__", "") for handler in handlers}

    assert {"start_command", "help_command"} <= registered
    assert all(isinstance(handler, MessageHandler) for handler in handlers)
