"""Constructing Kurigram clients.

Only the *identity* and safety-relevant parameters are set here; anything left
unset keeps Kurigram's default rather than a guessed value, so there is one
obvious place to look when the library changes its mind about a default.

Headless note: clients are constructed but never started here. Starting is the
caller's job, which keeps this module importable in a unit test.
"""

from __future__ import annotations

from typing import Any

from pyrogram import Client, enums

from app import paths
from app.config import Settings
from telegram.profiles import identity_kwargs

# Kurigram defaults this to 1. The speedups page recommends raising it for file
# transfers, which is most of what this bot does.
TRANSMISSIONS = 4


def _base_kwargs(settings: Settings) -> dict[str, Any]:
    return {
        "api_id": settings.tg_api_id.get_secret_value(),
        "api_hash": settings.tg_api_hash.get_secret_value(),
        "test_mode": settings.test_mode,
        "max_concurrent_transmissions": TRANSMISSIONS,
        **identity_kwargs(settings.client.active()),
    }


def build_bot(settings: Settings) -> Client:
    """The bot account itself.

    ``plugins`` is Kurigram's smart-plugin loader: it imports every module under
    ``handlers`` and collects the handlers they register, so adding a command means
    adding a file rather than editing a registry.
    """
    return Client(
        name="spideybot",
        workdir=str(paths.BOT_DIR),
        bot_token=settings.tg_bot_token.get_secret_value(),
        plugins=dict(root="handlers"),
        parse_mode=enums.ParseMode.MARKDOWN,
        **_base_kwargs(settings),
    )


def build_user(settings: Settings, user_id: int) -> Client:
    """A client that speaks as one *user*, backed by that user's stored session (D7).

    Two differences from the bot client, and both matter:

    * the session file is named by the user id and lives in ``SESSIONS_DIR`` (or its test-mode
      sibling), which is what makes a restart reconnect instead of asking for a code again;
    * ``no_updates`` is on. This client is a *fetcher* — it resolves and downloads media the user
      asks for — and a second dispatcher competing for the same account's updates would consume
      the ones the account's own clients expect. Nothing needs them: the magic word and TimerMedia
      arrive at the bot, not here.
    """
    directory = paths.sessions_dir(test_mode=settings.test_mode)
    return Client(
        name=str(user_id),
        workdir=str(directory),
        no_updates=True,
        **_base_kwargs(settings),
    )
