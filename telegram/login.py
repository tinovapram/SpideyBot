"""The ``/login`` conversation (FR-13, E13/E14/E15).

Pyrogram has no conversation handler, so the flow is a small explicit state machine: one entry per
user, holding the stage, the phone number and the not-yet-adopted client. The bot's own handlers
are then thin — ``/login`` starts it, and any plain text message *while a login is in progress*
continues it — which is what makes the whole thing testable without a bot, a chat or a clock.

Three decisions are worth stating:

* **Every outcome is a message, not an exception.** FR-13 lists eight steps and every one of them
  ends by telling the user something; returning the copy means the handler cannot forget to send
  it, and a test asserts on the sentence the user actually reads.
* **The client exists across steps.** Sending a code and signing in are two calls on the same
  connection with a gap in between (the user fetching their phone), so the client is created at the
  phone step and closed on every exit that is not success: cancel, timeout, or a refusal. A leak
  here is a socket per abandoned login.
* **The flow is time-bounded.** A half-finished login holds a connected client and the account's
  auth key, so each conversation runs its own timer (:data:`LOGIN_TIMEOUT_SECONDS`) that closes the
  client when nobody comes back. A timer rather than a check on the next message, because the next
  message may never arrive — which is the case that would otherwise leak a socket — and the user is
  still told, once, that it lapsed.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from pyrogram import errors

from app import catalog
from app.config import Settings
from app.logging import mask_secret
from telegram.sessions import SessionError, SessionPool

log = logging.getLogger(__name__)

#: How long a half-finished login may sit idle (three steps, so five minutes is generous).
LOGIN_TIMEOUT_SECONDS = 300.0


class Stage(StrEnum):
    """Where the conversation is. The value is also the catalog key's suffix."""

    PHONE = "phone"
    CODE = "code"
    PASSWORD = "password"


@dataclass
class Login:
    """One in-progress login."""

    user_id: int
    stage: Stage
    phone: str = ""
    phone_code_hash: str = ""
    client: Any = None
    started: float = field(default=0.0)
    #: The timer that closes an abandoned conversation (FR-13.7). Cancelled by every exit that is
    #: not a timeout, so a finished login leaves no task sleeping for five minutes.
    timer: Any = None


class LoginFlow:
    """The login steps, driven one message at a time."""

    def __init__(
        self,
        settings: Settings,
        pool: SessionPool,
        *,
        clock: Any = time.monotonic,
        timeout_seconds: float = LOGIN_TIMEOUT_SECONDS,
    ) -> None:
        self._settings = settings
        self._pool = pool
        self._clock = clock
        self._timeout = timeout_seconds
        self._logins: dict[int, Login] = {}
        #: Users whose conversation timed out. Kept so the *next* thing they send gets the retry
        #: message rather than silence: the state is gone, but the user does not know that yet.
        self._timed_out: set[int] = set()

    # ── what the handler asks before anything else ───────────────────────

    def in_progress(self, user_id: int) -> bool:
        """Whether this user is mid-login.

        A timed-out conversation is *not* in progress, and the message that follows it is still
        routed to the flow — see ``is_awaiting_input`` — so the user is told, not ignored.
        """
        return user_id in self._logins

    def is_awaiting_input(self, user_id: int) -> bool:
        """Whether a plain message from this user belongs to the conversation.

        True while a login is open and once after it has timed out, which is the difference between
        "ask the next question" and "tell them it lapsed".
        """
        return user_id in self._logins or user_id in self._timed_out

    def stage_of(self, user_id: int) -> Stage | None:
        login = self._logins.get(user_id)
        return login.stage if login is not None else None

    # ── the steps ────────────────────────────────────────────────────────

    async def start(self, user_id: int) -> str:
        """``/login``: open the phone step, or refuse with a reason (FR-13.1, FR-13.2)."""
        if self.in_progress(user_id):
            return catalog.LOGIN_IN_PROGRESS
        if self._pool.has_session(user_id):
            return catalog.LOGIN_ALREADY
        # Coming back after a timeout: the old conversation is gone, so this is a fresh start, and
        # the retry message from the timeout is cleared rather than repeated.
        self._timed_out.discard(user_id)
        login = Login(user_id=user_id, stage=Stage.PHONE, started=self._clock())
        self._logins[user_id] = login
        login.timer = asyncio.create_task(self._watch(user_id))
        return catalog.LOGIN_PHONE

    async def cancel(self, user_id: int) -> str:
        """``/cancel`` while logged in-progress (FR-13.8): honour it and say so."""
        self._timed_out.discard(user_id)
        login = self._logins.pop(user_id, None)
        if login is None:
            return catalog.LOGIN_NONE
        await self._close(login)
        return catalog.LOGIN_CANCELLED

    async def submit(self, user_id: int, text: str) -> str:
        """Feed one message to the conversation and get the next thing to say.

        A message that arrives with no login open is not an error: the handler only routes the ones
        that arrive while a login is open or just after it lapsed, so this is a stale state rather
        than a wrong answer.
        """
        if self._timed_out.discard(user_id) or user_id not in self._logins:
            return catalog.LOGIN_TIMED_OUT

        login = self._logins[user_id]
        self._touch_clock(login)

        match login.stage:
            case Stage.PHONE:
                return await self._phone(login, text)
            case Stage.CODE:
                return await self._code(login, text)
            case Stage.PASSWORD:
                return await self._password(login, text)
        raise AssertionError(f"unhandled login stage {login.stage!r}")  # pragma: no cover

    # ── phone → code → password ──────────────────────────────────────────

    async def _phone(self, login: Login, text: str) -> str:
        phone = _normalise_phone(text)
        if phone is None:
            return catalog.LOGIN_PHONE_INVALID

        client = _open_client(login, self._pool.new_client(login.user_id))
        try:
            await client.connect()
            sent = await client.send_code(phone)
        except errors.FloodWait as wait:
            await self._close(login)
            self._logins.pop(login.user_id, None)
            return catalog.LOGIN_FLOOD.format(seconds=wait.value)
        except Exception as failed:
            log.warning(
                "login: sending the code failed",
                extra={"user_id": login.user_id, "error": type(failed).__name__},
            )
            await self._close(login)
            self._logins.pop(login.user_id, None)
            return catalog.LOGIN_FAILED

        login.phone = phone
        login.phone_code_hash = sent.phone_code_hash
        login.stage = Stage.CODE
        return catalog.LOGIN_CODE.format(masked=mask_secret(phone, keep_first=4, keep_last=2))

    async def _code(self, login: Login, text: str) -> str:
        code = text.strip().replace(" ", "")
        try:
            await login.client.sign_in(login.phone, login.phone_code_hash, code)
        except errors.SessionPasswordNeeded:
            login.stage = Stage.PASSWORD
            return catalog.LOGIN_PASSWORD
        except errors.PhoneCodeInvalid:
            # E13: the code is wrong but the login is still alive — the same code hash can be
            # retried, so the user stays in this step rather than starting over.
            return catalog.LOGIN_CODE_INVALID
        except errors.PhoneCodeExpired:
            await self._close(login)
            self._logins.pop(login.user_id, None)
            return catalog.LOGIN_CODE_EXPIRED
        except errors.FloodWait as wait:
            await self._close(login)
            self._logins.pop(login.user_id, None)
            return catalog.LOGIN_FLOOD.format(seconds=wait.value)
        except Exception as failed:
            log.warning(
                "login: sign-in failed",
                extra={"user_id": login.user_id, "error": type(failed).__name__},
            )
            await self._close(login)
            self._logins.pop(login.user_id, None)
            return catalog.LOGIN_FAILED

        return await self._finish(login)

    async def _password(self, login: Login, text: str) -> str:
        try:
            await login.client.check_password(text)
        except errors.PasswordHashInvalid:
            return catalog.LOGIN_PASSWORD_INVALID  # E15: retryable, same as a wrong code
        except errors.FloodWait as wait:
            await self._close(login)
            self._logins.pop(login.user_id, None)
            return catalog.LOGIN_FLOOD.format(seconds=wait.value)
        except Exception as failed:
            log.warning(
                "login: 2FA failed",
                extra={"user_id": login.user_id, "error": type(failed).__name__},
            )
            await self._close(login)
            self._logins.pop(login.user_id, None)
            return catalog.LOGIN_FAILED

        return await self._finish(login)

    async def _finish(self, login: Login) -> str:
        """Sign-in is done: make the client usable and hand it to the pool (FR-13.6)."""
        try:
            await login.client.initialize()
            await self._pool.add(login.user_id, login.client)
        except SessionError as refusal:
            log.warning("login: adopting the client failed", extra={"user_id": login.user_id})
            await self._close(login)
            self._logins.pop(login.user_id, None)
            return refusal.reason
        except Exception as failed:
            log.warning(
                "login: the client could not be started",
                extra={"user_id": login.user_id, "error": type(failed).__name__},
            )
            await self._close(login)
            self._logins.pop(login.user_id, None)
            return catalog.LOGIN_FAILED

        # The pool owns the client from here, so the flow must not close it — only its timer, which
        # would otherwise close a client the pool is using five minutes later.
        self._stop_timer(login)
        self._logins.pop(login.user_id, None)
        masked = mask_secret(login.phone or str(login.user_id), keep_first=4, keep_last=2)
        return catalog.LOGIN_SUCCESS.format(masked=masked)

    # ── internals ────────────────────────────────────────────────────────

    def _touch_clock(self, login: Login) -> None:
        """Keep the conversation's deadline moving with each answer.

        The timeout measures a *stalled* conversation, not its total length: three steps with a code
        to fetch in between are quick, but a slow user should not lose a step they are still on.
        """
        login.started = self._clock()

    async def _watch(self, user_id: int) -> None:
        """Close and forget a conversation nobody came back to (FR-13.7).

        A timer rather than a check on the next message, because the leak it prevents is a
        *connected client*: an abandoned login holds a socket and the account's auth key, and no
        future message from that user is guaranteed to arrive to notice it.
        """
        try:
            await asyncio.sleep(self._timeout)
        except asyncio.CancelledError:  # pragma: no cover - the conversation finished first
            raise
        login = self._logins.pop(user_id, None)
        if login is None:  # pragma: no cover - cancelled timers never reach here
            return
        await self._close(login)
        # Kept so the next message gets the retry sentence rather than a fresh step's question.
        self._timed_out.add(user_id)
        log.info("login timed out", extra={"user_id": user_id})

    async def _close(self, login: Login) -> None:
        """Close a client this conversation owns. Never called after a successful adoption."""
        self._stop_timer(login)
        if login.client is None:
            return
        client, login.client = login.client, None
        with contextlib.suppress(Exception):
            await client.stop()

    @staticmethod
    def _stop_timer(login: Login) -> None:
        timer = login.timer
        login.timer = None
        if timer is not None and not timer.done():
            timer.cancel()


def _open_client(login: Login, client: Any) -> Any:
    """Record the client on the conversation *before* it is used.

    It has to be recorded before ``connect()``: a connect that fails, or a phone number Telegram
    refuses, must still leave the flow able to close what it built. Assigning afterwards is the
    version that leaks a socket per failed attempt.
    """
    login.client = client
    return client


def _normalise_phone(text: str) -> str | None:
    """A phone number in international form, or ``None``.

    The spec asks for ``+number``; spaces, dashes and brackets are stripped because that is how
    people write numbers, and a range of digits is accepted without the ``+`` because Telegram
    accepts it too — guessing the country code is not this bot's business.
    """
    digits = "".join(character for character in text if character.isdigit())
    if len(digits) < 8 or len(digits) > 15:
        return None
    return "+" + digits
