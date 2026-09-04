"""
/login and /logout handlers.

Multi-step conversation: phone → code → optional 2FA. The user's Telegram
session is persisted as a Telethon file session (``user_sessions/<id>.session``).
"""

from __future__ import annotations

import structlog
from telethon import Button, events
from telethon.errors import (
    FloodWaitError,
    PhoneCodeExpiredError,
    PhoneCodeInvalidError,
    SessionPasswordNeededError,
)
from telethon.tl.types import User

from core import sessions

logger = structlog.get_logger(__name__)

_login_locks: dict[int, bool] = {}


def register_login_handlers(bot) -> None:
    @bot.on(events.NewMessage(pattern=r"/login(?:@\w+)?\s*$"))
    async def login_handler(event):
        user_id = event.sender_id

        if user_id in _login_locks:
            await event.respond("⏳ A login flow is already in progress. Send /cancel to abort.")
            return

        if sessions.has_session(user_id):
            await event.respond(
                "✅ You are already logged in.\nUse /logout first to re-login with a different account."
            )
            return

        _login_locks[user_id] = True
        try:
            async with bot.conversation(event.chat_id, timeout=300) as conv:
                await conv.send_message(
                    "🔐 **Login to your Telegram account**\n\n"
                    "This lets SpideyBot access private/restricted content on your behalf.\n\n"
                    "⚠️ Your session is stored as an encrypted file session.\n"
                    "You can revoke access at any time with /logout.\n\n"
                    "Please send your **phone number** (e.g. `+1234567890`):",
                    buttons=[[Button.request_phone("📱 Send my number")]],
                )

                phone = _extract_phone(await conv.get_response())
                if phone is None or phone.lower() in ("/cancel", "cancel"):
                    await conv.send_message("❌ Login cancelled.", buttons=Button.clear())
                    return

                client = sessions.create_login_client(user_id)
                await client.connect()

                try:
                    await client.send_code_request(phone)
                except FloodWaitError as exc:
                    await client.disconnect()
                    await conv.send_message(
                        f"⏳ Rate limited by Telegram. Please try again in {exc.seconds} seconds.",
                        buttons=Button.clear(),
                    )
                    return
                except Exception as exc:
                    await client.disconnect()
                    logger.error("Login code request failed", user_id=user_id, error=str(exc))
                    await conv.send_message(
                        "❌ Failed to send verification code. Check your phone number and try again.",
                        buttons=Button.clear(),
                    )
                    return

                masked = phone[:4] + "****" + phone[-2:]
                await conv.send_message(
                    f"📩 Verification code sent to `{masked}`.\n\nPlease enter the **code** you received:",
                    buttons=Button.clear(),
                )

                code = (await conv.get_response()).text.strip().replace(" ", "")
                if code.lower() in ("/cancel", "cancel"):
                    await client.disconnect()
                    await conv.send_message("❌ Login cancelled.", buttons=Button.clear())
                    return
                if not code.isdigit() or len(code) < 4:
                    await client.disconnect()
                    await conv.send_message("❌ Invalid code format. Login aborted.", buttons=Button.clear())
                    return

                try:
                    user = await client.sign_in(phone, code)
                except SessionPasswordNeededError:
                    user = await _handle_2fa(conv, client, user_id)
                    if user is None:
                        return
                except PhoneCodeInvalidError:
                    await client.disconnect()
                    await conv.send_message("❌ Invalid code. Please try again with /login.", buttons=Button.clear())
                    return
                except PhoneCodeExpiredError:
                    await client.disconnect()
                    await conv.send_message("❌ Code expired. Please try again with /login.", buttons=Button.clear())
                    return
                except Exception as exc:
                    await client.disconnect()
                    logger.error("Login sign_in failed", user_id=user_id, error=str(exc))
                    await conv.send_message("❌ Login failed. Please try again later.", buttons=Button.clear())
                    return

                if not isinstance(user, User):
                    await client.disconnect()
                    await conv.send_message("❌ Unexpected login response. Please try again.", buttons=Button.clear())
                    return

                # Persist auth to the file session, then start the persistent client.
                try:
                    client.session.save()
                except Exception as exc:
                    logger.warning("Session save after login failed", error=str(exc))
                await client.disconnect()

                started = await sessions.start_client(user_id)
                message = (
                    f"✅ **Login successful!**\n\nLogged in as `{masked}`.\n\n"
                    "Use `/stop` to disconnect, or `/logout` to revoke."
                    if started
                    else f"✅ **Login successful!**\n\nLogged in as `{masked}`.\nSession saved. Use `/start` to connect."
                )
                await conv.send_message(message, buttons=Button.clear())
                logger.info("Login completed", user_id=user_id, client_started=started)

        except Exception as exc:
            logger.error("Login conversation failed", user_id=user_id, error=str(exc))
            try:
                await event.respond("❌ Login timed out or failed. Please try again with /login.")
            except Exception:
                pass
        finally:
            _login_locks.pop(user_id, None)

    @bot.on(events.NewMessage(pattern=r"/logout(?:@\w+)?\s*$"))
    async def logout_handler(event):
        removed = await sessions.remove_session(event.sender_id)
        if removed:
            await event.respond("🔓 Logged out successfully. Your session has been removed.")
        else:
            await event.respond("ℹ️ You are not currently logged in.")


async def _handle_2fa(conv, client, user_id):
    """Prompt for the 2FA password and complete sign-in. Returns a User or None."""
    await conv.send_message("🔒 Two-factor authentication is enabled.\n\nPlease enter your **2FA password**:")
    password = (await conv.get_response()).text

    if password.lower() in ("/cancel", "cancel"):
        await client.disconnect()
        await conv.send_message("❌ Login cancelled.", buttons=Button.clear())
        return None

    try:
        return await client.sign_in(password=password)
    except Exception as exc:
        await client.disconnect()
        logger.error("Login 2FA failed", user_id=user_id, error=str(exc))
        await conv.send_message("❌ Incorrect password or login failed. Try again with /login.", buttons=Button.clear())
        return None


def _extract_phone(event) -> str | None:
    if hasattr(event, "media") and event.media and hasattr(event.media, "phone_number"):
        return event.media.phone_number

    text = (event.text or "").strip()
    if not text:
        return None
    if text.startswith("+") and text[1:].isdigit() and len(text) >= 10:
        return text
    return text
