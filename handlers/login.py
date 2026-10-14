"""``/login`` — the Telegram account conversation (FR-13, E13/E14/E15, AC-7).

The handler is deliberately thin: it starts a conversation, and routes the *next* plain message into
it. All the decisions — which stage, which refusal, whether the client is kept or closed — live in
:mod:`telegram.login`, so they can be tested without a dispatcher.

Two filter details carry weight:

* the continuation handler runs only for a user who has a login open, and only for messages that are
  not commands. A command during a login belongs to its own handler (``/cancel`` aborts the login,
  ``/logout`` is unrelated), and a plain message from anybody else must reach whatever else handles
  it rather than being eaten by a conversation they are not in.
* the flow is looked up through :func:`get_flow`, which builds it from settings and the pool on
  first use and is replaceable — the same seam :func:`telegram.sessions.get_pool` gives the rest of
  the handlers.
"""

from __future__ import annotations

from pyrogram import Client, filters
from pyrogram.types import Message

from app import config
from handlers.filterkit import message_filter
from telegram import login as login_module
from telegram.sessions import get_pool

_FLOW: login_module.LoginFlow | None = None


def get_flow() -> login_module.LoginFlow:
    """The process-wide login conversation, built on first use."""
    global _FLOW
    if _FLOW is None:
        _FLOW = login_module.LoginFlow(config.get_settings(), get_pool())
    return _FLOW


def set_flow(flow: login_module.LoginFlow | None) -> None:
    """Install a flow (tests), or clear it so the next call builds a fresh one."""
    global _FLOW
    _FLOW = flow


def _is_login_step(*, user_id: int | None, text: str | None) -> bool:
    """Whether this message is an answer to a question the login flow asked."""
    if user_id is None or not text:
        return False
    if text.startswith("/"):
        return False
    return get_flow().is_awaiting_input(user_id)


def _step_filter() -> filters.Filter:
    """Whether this message is an answer to a question the login flow asked.

    Built through :func:`handlers.filterkit.message_filter`, which deals with how the installed
    Kurigram hands a custom filter its arguments — a filter is called as a bound method, so its
    predicate receives the filter, the client *and* the update. What matters here is only that a
    plain message from somebody not in a conversation reaches whatever else handles it, rather than
    being eaten by a login they are not in.
    """

    async def check(message: Message) -> bool:
        sender = message.from_user
        return _is_login_step(
            user_id=sender.id if sender else None,
            text=message.text,
        )

    return message_filter(check, "LoginStep")


@Client.on_message(filters.command("login") & filters.private)
async def login_command(client: Client, message: Message) -> None:
    sender = message.from_user
    if sender is None:
        return
    await message.reply_text(await get_flow().start(sender.id))


@Client.on_message(filters.private & _step_filter())
async def login_step(client: Client, message: Message) -> None:
    sender = message.from_user
    if sender is None:  # pragma: no cover - the filter already required a sender
        return
    await message.reply_text(await get_flow().submit(sender.id, message.text or ""))
