"""
SpideyBot — /login and /logout handlers.

Implements a multi-step conversation for users to authenticate with their
own Telegram account so the bot can access private/restricted content on
their behalf.

Flow:
  /login  → ask phone → ask code → (optional 2FA password) → store session
  /logout → deactivate stored session
"""

from __future__ import annotations

from enum import Enum, auto

import structlog
from telethon import events, Button
from telethon.errors import (
    SessionPasswordNeededError,
    PhoneCodeInvalidError,
    PhoneCodeExpiredError,
    FloodWaitError,
)
from telethon.sessions import StringSession
from telethon import TelegramClient

from spideybot import config, user_sessions

logger = structlog.get_logger(__name__)


# ─── Login conversation states ──────────────────────────────────────

class _LoginState(Enum):
    AWAITING_PHONE = auto()
    AWAITING_CODE = auto()
    AWAITING_PASSWORD = auto()


# Per-user transient state — keyed by sender_id
_login_states: dict[int, _LoginState] = {}
_login_clients: dict[int, TelegramClient] = {}
_login_phones: dict[int, str] = {}


# ─── Command: /login ────────────────────────────────────────────────

def register_login_handlers(bot) -> None:
    """Register /login, /logout and the transient conversation handlers."""

    # ── /login entry point ──────────────────────────────────────────

    @bot.on(events.NewMessage(pattern=r"/login(?:@\w+)?\s*$"))
    async def login_handler(event):
        user_id = event.sender_id

        # If already logged in, show status
        existing = user_sessions.get_or_none(user_id)
        if existing:
            await event.respond(
                "✅ You are already logged in.\n"
                "Use /logout first if you want to re-login with a different account."
            )
            return

        await event.respond(
            "🔐 **Login to your Telegram account**\n\n"
            "This lets SpideyBot access private/restricted content on your behalf.\n\n"
            "⚠️ Your session is encrypted and stored securely.\n"
            "You can revoke access at any time with /logout.\n\n"
            "Please send your **phone number** (e.g. `+1234567890`):"
        )
        _login_states[user_id] = _LoginState.AWAITING_PHONE

    # ── /logout command ─────────────────────────────────────────────

    @bot.on(events.NewMessage(pattern=r"/logout(?:@\w+)?\s*$"))
    async def logout_handler(event):
        user_id = event.sender_id
        removed = user_sessions.remove(user_id)
        if removed:
            await event.respond("🔓 Logged out successfully. Your session has been removed.")
        else:
            await event.respond("ℹ️ You are not currently logged in.")

    # ── Transient message handler for the login conversation ────────

    @bot.on(events.NewMessage)
    async def _login_conversation(event):
        user_id = event.sender_id
        state = _login_states.get(user_id)
        if state is None:
            return  # not in a login flow

        text = event.text.strip()

        # Allow user to cancel at any step
        if text.lower() in ("/cancel", "cancel"):
            await _cleanup(user_id)
            await event.respond("❌ Login cancelled.")
            return

        if state == _LoginState.AWAITING_PHONE:
            # Basic phone validation
            if not text.startswith("+") or not text[1:].isdigit() or len(text) < 10:
                await event.respond("❌ Invalid phone number. Must start with `+` and contain digits.\n\nExample: `+1234567890`\n\nOr send /cancel to abort.")
                return

            phone = text
            _login_phones[user_id] = phone

            try:
                client = TelegramClient(
                    StringSession(),
                    int(config.TG_API_ID),
                    config.TG_API_HASH,
                )
                await client.connect()

                sent_code = await client.send_code_request(phone)
                _login_clients[user_id] = client
                _login_states[user_id] = _LoginState.AWAITING_CODE

                # Mask phone for privacy in the prompt
                masked = phone[:4] + "****" + phone[-2:]
                await event.respond(
                    f"📨 Verification code sent to `{masked}`.\n\n"
                    "Please enter the **code** you received:"
                )
                logger.info("Login code sent", user_id=user_id, phone=masked)

            except FloodWaitError as e:
                await _cleanup(user_id)
                await event.respond(
                    f"⏳ Rate limited by Telegram. Please try again in {e.seconds} seconds."
                )
                logger.warning("Login flood wait", user_id=user_id, seconds=e.seconds)
            except Exception as e:
                await _cleanup(user_id)
                await event.respond(
                    "❌ Failed to send verification code. Please try again later."
                )
                logger.error("Login code request failed", user_id=user_id, error=str(e))

        elif state == _LoginState.AWAITING_CODE:
            code = text.replace(" ", "")
            if not code.isdigit() or len(code) < 4:
                await event.respond("❌ Invalid code format. Please enter the numeric code.\n\nOr send /cancel to abort.")
                return

            client = _login_clients.get(user_id)
            phone = _login_phones.get(user_id)
            if not client or not phone:
                await _cleanup(user_id)
                await event.respond("❌ Session expired. Please start over with /login.")
                return

            try:
                await client.sign_in(phone, code)
                await _finish_login(user_id, client, event)

            except SessionPasswordNeededError:
                _login_states[user_id] = _LoginState.AWAITING_PASSWORD
                await event.respond(
                    "🔒 Two-factor authentication is enabled.\n\n"
                    "Please enter your **2FA password**:"
                )
                logger.info("Login requires 2FA", user_id=user_id)

            except PhoneCodeInvalidError:
                await event.respond(
                    "❌ Invalid code. Please try again.\n\nOr send /cancel to abort."
                )
                logger.warning("Login invalid code", user_id=user_id)

            except PhoneCodeExpiredError:
                await _cleanup(user_id)
                await event.respond(
                    "❌ Code expired. Please start over with /login."
                )
                logger.warning("Login code expired", user_id=user_id)

        elif state == _LoginState.AWAITING_PASSWORD:
            password = text
            client = _login_clients.get(user_id)
            if not client:
                await _cleanup(user_id)
                await event.respond("❌ Session expired. Please start over with /login.")
                return

            try:
                await client.sign_in(password=password)
                await _finish_login(user_id, client, event)

            except Exception as e:
                await _cleanup(user_id)
                await event.respond(
                    "❌ Incorrect password or login failed. Please try again with /login."
                )
                logger.error("Login 2FA failed", user_id=user_id, error=str(e))


# ─── Helpers ────────────────────────────────────────────────────────

async def _finish_login(user_id: int, client: TelegramClient, event) -> None:
    """Save the session and clean up the conversation state."""
    phone = _login_phones.get(user_id, "unknown")

    # Export the session string before disconnecting
    session_string = client.session.save()

    user_sessions.save(user_id, phone, session_string)

    # Disconnect the temporary login client, then start a persistent one
    await _cleanup(user_id)
    started = await user_sessions.start_client(user_id)

    masked = phone[:4] + "****" + phone[-2:]
    if started:
        await event.respond(
            "✅ **Login successful!**\n\n"
            f"Logged in as `{masked}`.\n\n"
            "Use `/stop` to disconnect, or `/logout` to revoke."
        )
    else:
        await event.respond(
            "✅ **Login successful!**\n\n"
            f"Logged in as `{masked}`.\n"
            "Session saved. Use `/start` to connect your account."
        )
    logger.info("Login completed", user_id=user_id, client_started=started)


async def _cleanup(user_id: int) -> None:
    """Disconnect temp client and clear conversation state."""
    client = _login_clients.pop(user_id, None)
    if client:
        try:
            await client.disconnect()
        except Exception:
            pass
    _login_states.pop(user_id, None)
    _login_phones.pop(user_id, None)
