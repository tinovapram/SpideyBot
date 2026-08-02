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

import structlog
from telethon import events, Button
from telethon.errors import (
    SessionPasswordNeededError,
    PhoneCodeInvalidError,
    PhoneCodeExpiredError,
    FloodWaitError,
)
from telethon.tl.types import User

from spideybot import config, user_sessions

logger = structlog.get_logger(__name__)

# Per-user lock to prevent concurrent login attempts
_login_locks: dict[int, bool] = {}


def register_login_handlers(bot) -> None:
    """Register /login, /logout handlers."""

    @bot.on(events.NewMessage(pattern=r"/login(?:@\w+)?\s*$"))
    async def login_handler(event):
        user_id = event.sender_id

        # Prevent concurrent login attempts
        if user_id in _login_locks:
            await event.respond(
                "\u23F3 A login flow is already in progress. Send /cancel to abort."
            )
            return

        # If already logged in, show status
        existing = user_sessions.get_or_none(user_id)
        if existing:
            await event.respond(
                "\u2705 You are already logged in.\n"
                "Use /logout first if you want to re-login with a different account."
            )
            return

        _login_locks[user_id] = True

        try:
            async with bot.conversation(event.chat_id, timeout=300) as conv:
                # Step 1: Phone number
                await conv.send_message(
                    "\U0001F510 **Login to your Telegram account**\n\n"
                    "This lets SpideyBot access private/restricted content on your behalf.\n\n"
                    "\u26A0\uFE0F Your session is encrypted and stored securely.\n"
                    "You can revoke access at any time with /logout.\n\n"
                    "Please send your **phone number** (e.g. `+1234567890`):",
                    buttons=[[Button.request_phone("\U0001F4F1 Send my number")]],
                )

                result = await conv.get_response()
                phone = _extract_phone(result)

                if phone is None or phone.lower() in ("/cancel", "cancel"):
                    await conv.send_message(
                        "\u274C Login cancelled.", buttons=Button.clear()
                    )
                    return

                # Step 2: Send code request
                client = _create_temp_client()
                await client.connect()

                try:
                    await client.send_code_request(phone)
                except FloodWaitError as e:
                    await client.disconnect()
                    await conv.send_message(
                        f"\u23F3 Rate limited by Telegram. Please try again in {e.seconds} seconds.",
                        buttons=Button.clear(),
                    )
                    return
                except Exception as e:
                    await client.disconnect()
                    logger.error(
                        "Login code request failed", user_id=user_id, error=str(e)
                    )
                    await conv.send_message(
                        "\u274C Failed to send verification code. Please check your phone number and try again.",
                        buttons=Button.clear(),
                    )
                    return

                masked = phone[:4] + "****" + phone[-2:]
                await conv.send_message(
                    f"\U0001F4E9 Verification code sent to `{masked}`.\n\n"
                    "Please enter the **code** you received:",
                    buttons=Button.clear(),
                )
                logger.info("Login code sent", user_id=user_id, phone=masked)

                # Step 3: Enter code
                result = await conv.get_response()
                code = result.text.strip().replace(" ", "")

                if code.lower() in ("/cancel", "cancel"):
                    await client.disconnect()
                    await conv.send_message(
                        "\u274C Login cancelled.", buttons=Button.clear()
                    )
                    return

                if not code.isdigit() or len(code) < 4:
                    await client.disconnect()
                    await conv.send_message(
                        "\u274C Invalid code format. Login aborted.",
                        buttons=Button.clear(),
                    )
                    return

                # Step 4: Sign in
                try:
                    user = await client.sign_in(phone, code)
                except SessionPasswordNeededError:
                    # 2FA required
                    await conv.send_message(
                        "\U0001F512 Two-factor authentication is enabled.\n\n"
                        "Please enter your **2FA password**:",
                    )
                    result = await conv.get_response()
                    password = result.text

                    if password.lower() in ("/cancel", "cancel"):
                        await client.disconnect()
                        await conv.send_message(
                            "\u274C Login cancelled.", buttons=Button.clear()
                        )
                        return

                    try:
                        user = await client.sign_in(password=password)
                    except Exception as e:
                        await client.disconnect()
                        logger.error("Login 2FA failed", user_id=user_id, error=str(e))
                        await conv.send_message(
                            "\u274C Incorrect password or login failed. Please try again with /login.",
                            buttons=Button.clear(),
                        )
                        return

                except PhoneCodeInvalidError:
                    await client.disconnect()
                    await conv.send_message(
                        "\u274C Invalid code. Please try again with /login.",
                        buttons=Button.clear(),
                    )
                    return

                except PhoneCodeExpiredError:
                    await client.disconnect()
                    await conv.send_message(
                        "\u274C Code expired. Please try again with /login.",
                        buttons=Button.clear(),
                    )
                    return

                except Exception as e:
                    await client.disconnect()
                    logger.error(
                        "Login sign_in failed", user_id=user_id, error=str(e)
                    )
                    await conv.send_message(
                        "\u274C Login failed. Please try again later.",
                        buttons=Button.clear(),
                    )
                    return

                # Step 5: Save session
                if isinstance(user, User):
                    session_string = client.session.save()
                    user_sessions.save(user_id, phone, session_string)
                    await client.disconnect()

                    # Start the persistent client
                    started = await user_sessions.start_client(user_id)

                    if started:
                        await conv.send_message(
                            "\u2705 **Login successful!**\n\n"
                            f"Logged in as `{masked}`.\n\n"
                            "Use `/stop` to disconnect, or `/logout` to revoke.",
                            buttons=Button.clear(),
                        )
                    else:
                        await conv.send_message(
                            "\u2705 **Login successful!**\n\n"
                            f"Logged in as `{masked}`.\n"
                            "Session saved. Use `/start` to connect your account.",
                            buttons=Button.clear(),
                        )
                    logger.info(
                        "Login completed", user_id=user_id, client_started=started
                    )
                else:
                    await client.disconnect()
                    await conv.send_message(
                        "\u274C Unexpected login response. Please try again.",
                        buttons=Button.clear(),
                    )

        except Exception as e:
            logger.error("Login conversation failed", user_id=user_id, error=str(e))
            try:
                await event.respond(
                    "\u274C Login timed out or failed. Please try again with /login."
                )
            except Exception:
                pass
        finally:
            _login_locks.pop(user_id, None)

    # /logout command

    @bot.on(events.NewMessage(pattern=r"/logout(?:@\w+)?\s*$"))
    async def logout_handler(event):
        user_id = event.sender_id
        removed = user_sessions.remove(user_id)
        if removed:
            await event.respond(
                "\U0001F513 Logged out successfully. Your session has been removed."
            )
        else:
            await event.respond(
                "\u2139\uFE0F You are not currently logged in."
            )


# Helpers


def _create_temp_client():
    """Create a temporary TelegramClient for the login flow."""
    from telethon import TelegramClient
    from telethon.sessions import StringSession

    return TelegramClient(
        StringSession(),
        int(config.TG_API_ID),
        config.TG_API_HASH,
    )


def _extract_phone(event) -> str | None:
    """Extract phone number from a message event.

    Supports:
      - Text messages with phone number (e.g. +1234567890)
      - Phone request button responses (media.phone_number)
    """
    # Check if user pressed the Send my number button
    if hasattr(event, "media") and event.media and hasattr(event.media, "phone_number"):
        return event.media.phone_number

    # Otherwise treat as text
    text = (event.text or "").strip()
    if not text:
        return None

    # Basic phone validation: must start with + and be digits
    if text.startswith("+") and text[1:].isdigit() and len(text) >= 10:
        return text

    return text  # return as-is for cancel detection
