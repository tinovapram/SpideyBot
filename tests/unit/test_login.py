"""The ``/login`` conversation (FR-13, E13/E14/E15).

Driven message by message against a fake client, which is the level the flow was written for: every
step's outcome is a sentence, every error has its own sentence, and the interesting questions are
which sentence and whether the client was kept or closed. The one thing this cannot prove is that
Telegram accepts a real code — that is the live suite's job (D19).
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from pyrogram import errors

from app import catalog, config, paths
from app.config import Settings
from telegram import sessions
from telegram.login import LOGIN_TIMEOUT_SECONDS, LoginFlow, Stage

USER = 77
PHONE = "+15551234567"
assert LOGIN_TIMEOUT_SECONDS > 0, "the default timeout is a positive number of seconds"


class Clock:
    def __init__(self) -> None:
        self.now = 500.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class FakeLoginClient:
    """A client that walks the login steps, and can be told where to fail."""

    def __init__(
        self,
        *,
        fail_code: Exception | None = None,
        fail_password: Exception | None = None,
        fail_password_attempts: int = 1,
        needs_password: bool = False,
        fail_send_code: Exception | None = None,
    ) -> None:
        self.is_connected = False
        self.is_initialized = False
        self.sent_to: list[str] = []
        self.signed_in_with: list[tuple[str, str, str]] = []
        self.passwords: list[str] = []
        self.stopped = False
        self._fail_code = fail_code
        self._fail_password = fail_password
        #: How many password attempts fail before one succeeds, so a retry can be tested without
        #: changing the fake between calls.
        self._fail_password_attempts = fail_password_attempts
        self._needs_password = needs_password
        self._fail_send_code = fail_send_code

    async def connect(self) -> bool:
        self.is_connected = True
        return True

    async def send_code(self, phone: str) -> Any:
        if self._fail_send_code is not None:
            raise self._fail_send_code
        self.sent_to.append(phone)
        return _SentCode(hash="hash-123")

    async def sign_in(self, phone: str, phone_code_hash: str, code: str) -> Any:
        self.signed_in_with.append((phone, phone_code_hash, code))
        if self._fail_code is not None:
            raise self._fail_code
        if self._needs_password:
            raise errors.SessionPasswordNeeded()
        return "user"

    async def check_password(self, password: str) -> Any:
        self.passwords.append(password)
        if self._fail_password is not None and len(self.passwords) <= self._fail_password_attempts:
            raise self._fail_password
        return "user"

    async def initialize(self) -> None:
        self.is_initialized = True

    async def stop(self) -> None:
        self.stopped = True
        self.is_connected = False

    async def log_out(self) -> None:
        self.stopped = True


class _SentCode:
    def __init__(self, *, hash: str) -> None:
        self.phone_code_hash = hash


class Factory:
    def __init__(self, client: FakeLoginClient) -> None:
        self.client = client
        self.built = 0

    def __call__(self, _user_id: int) -> FakeLoginClient:
        self.built += 1
        return self.client


@pytest.fixture
def settings(fake_secrets: dict[str, str], tmp_path: Path, monkeypatch) -> Settings:
    """Settings whose session directory is a temp dir, so nothing lands in ``data/``."""
    directory = tmp_path / "sessions"
    directory.mkdir()
    monkeypatch.setattr(paths, "DATA_DIR", tmp_path)
    monkeypatch.setattr(paths, "SESSIONS_DIR", directory)
    monkeypatch.setattr(paths, "TEST_SESSIONS_DIR", directory / "test")
    return config.build_settings(**fake_secrets)


def flow_for(
    settings: Settings, client: FakeLoginClient | None = None
) -> tuple[LoginFlow, Factory, Clock]:
    fake = client or FakeLoginClient()
    factory = Factory(fake)
    clock = Clock()
    pool = sessions.SessionPool(settings, factory=factory, clock=clock)
    return LoginFlow(settings, pool, clock=clock), factory, clock


# ── the three steps in order ────────────────────────────────────────────


async def test_login_asks_for_the_phone_first(settings) -> None:
    flow, _, _ = flow_for(settings)

    reply = await flow.start(USER)

    assert reply == catalog.LOGIN_PHONE
    assert "📱" not in reply, "the spec's copy is used, not V5's button"
    assert flow.stage_of(USER) is Stage.PHONE


async def test_a_phone_number_sends_a_code_and_masks_it_in_the_reply(settings) -> None:
    flow, factory, _ = flow_for(settings)
    await flow.start(USER)

    reply = await flow.submit(USER, PHONE)

    assert factory.client.sent_to == [PHONE]
    assert flow.stage_of(USER) is Stage.CODE
    assert "+155" in reply and "7" in reply, "the mask keeps the head and tail"
    assert PHONE not in reply, "the full number is not echoed back"


async def test_a_code_finishes_the_login_when_there_is_no_2fa(settings) -> None:
    flow, factory, _ = flow_for(settings)
    await flow.start(USER)
    await flow.submit(USER, PHONE)

    reply = await flow.submit(USER, "12345")

    assert factory.client.signed_in_with == [(PHONE, "hash-123", "12345")]
    assert factory.client.is_initialized, "the client is made usable before it is adopted"
    assert catalog.LOGIN_SUCCESS.split("\n")[0] in reply
    assert flow.stage_of(USER) is None, "the conversation is over"


async def test_a_two_factor_account_is_asked_for_its_password(settings) -> None:
    flow, factory, _ = flow_for(settings, FakeLoginClient(needs_password=True))
    await flow.start(USER)
    await flow.submit(USER, PHONE)

    prompt = await flow.submit(USER, "12345")

    assert prompt == catalog.LOGIN_PASSWORD
    assert flow.stage_of(USER) is Stage.PASSWORD

    done = await flow.submit(USER, "correct horse")

    assert factory.client.passwords == ["correct horse"]
    assert "Login successful" in done


async def test_a_password_is_not_stored_anywhere_the_reply_can_show(settings) -> None:
    """The 2FA password is the user's account password; it must not come back in a message."""
    flow, _, _ = flow_for(settings, FakeLoginClient(needs_password=True))
    await flow.start(USER)
    await flow.submit(USER, PHONE)
    await flow.submit(USER, "12345")

    done = await flow.submit(USER, "hunter2")

    assert "hunter2" not in done


async def test_the_success_message_names_the_account_masked(settings) -> None:
    flow, _, _ = flow_for(settings)
    await flow.start(USER)
    await flow.submit(USER, PHONE)

    done = await flow.submit(USER, "12345")

    assert "Logged in as `+155" in done
    assert PHONE not in done


# ── refusals before anything starts (FR-13.1, FR-13.2) ──────────────────


async def test_a_login_already_in_progress_is_refused(settings) -> None:
    flow, _, _ = flow_for(settings)
    await flow.start(USER)

    assert await flow.start(USER) == catalog.LOGIN_IN_PROGRESS


async def test_a_user_with_a_stored_session_is_sent_to_logout_first(settings) -> None:
    """/login is not a way to replace a session: /logout is (FR-13.2, D10)."""
    flow, factory, _ = flow_for(settings)
    (paths.SESSIONS_DIR / f"{USER}.session").write_bytes(b"sqlite")

    reply = await flow.start(USER)

    assert reply == catalog.LOGIN_ALREADY
    assert factory.built == 0


# ── the error cases the spec names ──────────────────────────────────────


async def test_a_bad_number_is_rejected_without_building_a_client(settings) -> None:
    flow, factory, _ = flow_for(settings)
    await flow.start(USER)

    reply = await flow.submit(USER, "not a number")

    assert reply == catalog.LOGIN_PHONE_INVALID
    assert factory.built == 0, "nothing is connected to check a phone number's shape"
    assert flow.stage_of(USER) is Stage.PHONE, "the user may simply retype it"


@pytest.mark.parametrize("text", ["+1 555 123 4567", "+1-555-123-4567", "15551234567"])
async def test_a_number_is_normalised_however_it_is_written(settings, text: str) -> None:
    flow, factory, _ = flow_for(settings)
    await flow.start(USER)

    await flow.submit(USER, text)

    assert factory.client.sent_to == [PHONE]


async def test_an_invalid_code_keeps_the_login_alive(settings) -> None:
    """E13. A wrong code is retryable against the same code hash, so the user stays here."""
    flow, factory, _ = flow_for(settings, FakeLoginClient(fail_code=errors.PhoneCodeInvalid()))
    await flow.start(USER)
    await flow.submit(USER, PHONE)

    reply = await flow.submit(USER, "00000")

    assert reply == catalog.LOGIN_CODE_INVALID
    assert flow.stage_of(USER) is Stage.CODE
    assert not factory.client.stopped, "the client is kept: retrying needs it"


async def test_an_expired_code_ends_the_login_and_closes_the_client(settings) -> None:
    flow, factory, _ = flow_for(settings, FakeLoginClient(fail_code=errors.PhoneCodeExpired()))
    await flow.start(USER)
    await flow.submit(USER, PHONE)

    reply = await flow.submit(USER, "12345")

    assert reply == catalog.LOGIN_CODE_EXPIRED
    assert flow.stage_of(USER) is None
    assert factory.client.stopped, "an expired code cannot be retried, so the socket is released"


async def test_a_wrong_password_is_retryable(settings) -> None:
    """E15, exactly as E13 is: the spec asks for a retry, not a restart."""
    flow, factory, _ = flow_for(
        settings, FakeLoginClient(needs_password=True, fail_password=errors.PasswordHashInvalid())
    )
    await flow.start(USER)
    await flow.submit(USER, PHONE)
    await flow.submit(USER, "12345")

    reply = await flow.submit(USER, "wrong")

    assert reply == catalog.LOGIN_PASSWORD_INVALID
    assert flow.stage_of(USER) is Stage.PASSWORD
    assert not factory.client.stopped

    fixed = await flow.submit(USER, "right")

    assert "Login successful" in fixed


async def test_a_flood_wait_reports_its_seconds(settings) -> None:
    """E14."""
    flow, factory, _ = flow_for(
        settings, FakeLoginClient(fail_send_code=errors.FloodWait(value=30))
    )
    await flow.start(USER)

    reply = await flow.submit(USER, PHONE)

    assert "30 seconds" in reply
    assert flow.stage_of(USER) is None
    assert factory.client.stopped


async def test_a_phone_number_telegram_refuses_ends_the_login(settings) -> None:
    flow, factory, _ = flow_for(
        settings, FakeLoginClient(fail_send_code=errors.PhoneNumberInvalid())
    )
    await flow.start(USER)

    reply = await flow.submit(USER, PHONE)

    assert reply == catalog.LOGIN_FAILED
    assert factory.client.stopped


# ── cancellation and timeouts (FR-13.7, FR-13.8) ────────────────────────


async def test_cancelling_says_so_and_closes_the_client(settings) -> None:
    flow, factory, _ = flow_for(settings)
    await flow.start(USER)
    await flow.submit(USER, PHONE)

    reply = await flow.cancel(USER)

    assert reply == catalog.LOGIN_CANCELLED
    assert flow.stage_of(USER) is None
    assert factory.client.stopped


async def test_cancelling_when_nothing_is_in_progress_says_that(settings) -> None:
    flow, _, _ = flow_for(settings)

    assert await flow.cancel(USER) == catalog.LOGIN_NONE


async def test_a_login_that_sits_idle_times_out_and_is_closed(settings) -> None:
    """FR-13.7, and the socket it prevents: nobody coming back must not leave a client connected.

    Driven with a real (tiny) timeout rather than a fake clock, because what is under test is the
    timer itself — a check-on-the-next-message design would pass a clock-based test and still leak.
    """
    fake = FakeLoginClient()
    pool = sessions.SessionPool(settings, factory=Factory(fake))
    flow = LoginFlow(settings, pool, timeout_seconds=0.05)
    await flow.start(USER)
    await flow.submit(USER, PHONE)

    await asyncio.sleep(0.2)

    assert not flow.in_progress(USER)
    assert fake.stopped, "an abandoned conversation must not leave a socket behind"
    assert flow.is_awaiting_input(USER), "the lapsed conversation still owes the user an answer"

    reply = await flow.submit(USER, "12345")

    assert reply == catalog.LOGIN_TIMED_OUT
    assert fake.signed_in_with == [], "a timed-out code is not attempted"
    assert not flow.is_awaiting_input(USER), "and the answer is given once"
    assert await flow.submit(USER, "12345") == catalog.LOGIN_TIMED_OUT


async def test_a_message_with_no_login_open_is_a_timeout_not_a_wrong_code(settings) -> None:
    """The filter routes only live conversations, so this is a stale state, not a bad code."""
    flow, factory, _ = flow_for(settings)

    reply = await flow.submit(USER, "12345")

    assert reply == catalog.LOGIN_TIMED_OUT
    assert factory.built == 0


# ── the pool's view of a completed login ────────────────────────────────


async def test_a_finished_login_leaves_the_client_live_in_the_pool(settings) -> None:
    flow, factory, _ = flow_for(settings)
    pool = flow._pool  # noqa: SLF001 - the flow's pool is the seam under test
    await flow.start(USER)
    await flow.submit(USER, PHONE)

    await flow.submit(USER, "12345")

    assert pool.is_live(USER)
    assert await pool.get(USER) is factory.client
    assert not factory.client.stopped, "the pool owns it now; the flow must not close it"
