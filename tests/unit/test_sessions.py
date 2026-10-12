"""The user-session pool (D7, D10, D21, FR-13).

The pool is exercised against fake clients, and that is the right level for most of it: the
decisions worth testing are *which* client is open, when it is closed, and what the caller is told
— none of which involves Telegram. The two things a fake cannot prove are whether a real session
file round-trips through a restart and whether ``log_out()`` really revokes; those belong to the
live suite, and the code is written so that they are the only things left for it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from pyrogram import errors

from app import config, paths
from app.config import Settings
from telegram import magic, sessions, timer

USER = 501


class FakeUserClient:
    """A user client that records what the pool did to it.

    The lifecycle mirrors Kurigram's: ``connect()`` answers whether the session is *authorized*,
    ``get_me()`` is the proof that it still works, ``initialize()`` is what makes the client usable,
    and ``stop()`` is terminate plus disconnect — the only call that closes an initialized client.
    """

    def __init__(
        self,
        *,
        authorized: bool = True,
        connect_error: Exception | None = None,
        me_error: Exception | None = None,
        me: Any = "user",
    ) -> None:
        self.is_connected = False
        self.is_initialized = False
        self.connects = 0
        self.stops = 0
        self.closed = False
        #: ``(handler, group)`` pairs, so a test can ask what a live session is *listening* for —
        #: FR-5's magic word and FR-6's auto-save are attached here, and nothing else would.
        self.handlers: list[tuple[Any, Any]] = []
        self._authorized = authorized
        self._connect_error = connect_error
        self._me_error = me_error
        self._me = me

    def add_handler(self, handler: Any, group: Any = None) -> None:
        self.handlers.append((handler, group))

    async def connect(self) -> bool:
        self.connects += 1
        if self._connect_error is not None:
            raise self._connect_error
        self.is_connected = True
        return self._authorized

    async def get_me(self) -> Any:
        if self._me_error is not None:
            raise self._me_error
        return self._me

    async def initialize(self) -> None:
        if not self.is_connected:
            raise ConnectionError("Can't initialize a disconnected client")
        self.is_initialized = True

    async def stop(self) -> None:
        self.stops += 1
        self.is_initialized = False
        self.is_connected = False
        self.closed = True


class FakeFactory:
    """Hands out named fakes, so a test can count and inspect them."""

    def __init__(self, **kwargs: Any) -> None:
        self._kwargs = kwargs
        self.clients: dict[int, FakeUserClient] = {}

    def __call__(self, user_id: int) -> FakeUserClient:
        client = FakeUserClient(**self._kwargs)
        self.clients[user_id] = client
        return client


class Clock:
    """A clock the test moves, because idle time is otherwise a 15-minute wait."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def settings(fake_secrets: dict[str, str]) -> Settings:
    """Settings with a small cap, so capacity can be reached without 50 clients."""
    return config.build_settings(**fake_secrets, runtime={"session_max_live": 2})


@pytest.fixture
def stored(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, settings: Settings) -> Path:
    """A session directory of our own, with one user's file already in it.

    ``paths`` resolves from the project root, so the directory is redirected rather than written
    into ``data/`` — a test that creates real session files would leave credentials lying around.
    """
    directory = tmp_path / "sessions"
    directory.mkdir()
    monkeypatch.setattr(paths, "DATA_DIR", tmp_path)
    monkeypatch.setattr(paths, "SESSIONS_DIR", directory)
    monkeypatch.setattr(paths, "TEST_SESSIONS_DIR", directory / "test")
    (directory / f"{USER}.session").write_bytes(b"sqlite")
    return directory


def pool_for(
    settings: Settings, **factory_kwargs: Any
) -> tuple[sessions.SessionPool, FakeFactory, Clock]:
    factory = FakeFactory(**factory_kwargs)
    clock = Clock()
    return sessions.SessionPool(settings, factory=factory, clock=clock), factory, clock


# ── admission, and what it means to have no session ─────────────────────


async def test_a_user_with_no_stored_session_is_refused_by_name(settings, stored) -> None:
    pool, factory, _ = pool_for(settings)

    with pytest.raises(sessions.SessionError, match="/login"):
        await pool.get(999)

    assert factory.clients == {}, "no client is built for a user who never logged in"


async def test_a_stored_session_connects_on_demand(settings, stored) -> None:
    pool, factory, _ = pool_for(settings)

    client = await pool.get(USER)

    assert client is factory.clients[USER]
    assert client.connects == 1
    assert pool.is_live(USER)
    assert pool.badge(USER) == (True, True)


async def test_connecting_twice_reuses_the_live_client(settings, stored) -> None:
    """R3: the socket is the cost, so a second request must not open a second one."""
    pool, factory, _ = pool_for(settings)

    first = await pool.get(USER)
    second = await pool.get(USER)

    assert first is second
    assert first.connects == 1


async def test_has_session_is_about_the_file_not_the_socket(settings, stored) -> None:
    pool, _, _ = pool_for(settings)

    assert pool.has_session(USER)
    assert not pool.is_live(USER)
    assert pool.badge(USER) == (True, False), "the 🟡 badge: saved, not running"


# ── a dead session is deleted, not retried forever ──────────────────────


async def test_a_revoked_session_is_deleted_and_reported_as_expired(settings, stored) -> None:
    """Otherwise /account promises a session that can never connect (§7.6's badges)."""
    pool, _, _ = pool_for(settings, me_error=errors.AuthKeyUnregistered())

    with pytest.raises(sessions.SessionError) as caught:
        await pool.get(USER)

    assert caught.value.expired
    assert "/login" in caught.value.reason
    assert not pool.has_session(USER), "the unusable file is gone"
    assert not pool.is_live(USER)


async def test_a_session_that_connects_but_answers_with_nobody_is_treated_as_dead(
    settings, stored
) -> None:
    """``connect()`` succeeding is not evidence: the proof is a request."""
    pool, _, _ = pool_for(settings, me=None)

    with pytest.raises(sessions.SessionError) as caught:
        await pool.get(USER)

    assert caught.value.expired
    assert not pool.has_session(USER)


async def test_a_transport_failure_keeps_the_session_file(settings, stored) -> None:
    """A network blip must not cost the user their login."""
    pool, _, _ = pool_for(settings, connect_error=ConnectionError("no route"))

    with pytest.raises(sessions.SessionError) as caught:
        await pool.get(USER)

    assert not caught.value.expired
    assert pool.has_session(USER), "still stored, so the next attempt can use it"


async def test_a_connect_that_fails_closes_the_client_it_built(settings, stored) -> None:
    pool, factory, _ = pool_for(settings, connect_error=ConnectionError("no route"))

    with pytest.raises(sessions.SessionError):
        await pool.get(USER)

    assert factory.clients[USER].closed, "a failed connect must not leak a socket"


async def test_a_session_telegram_no_longer_authorizes_is_deleted(settings, stored) -> None:
    """The first thing a revoked key changes is ``connect()``'s answer, with no error at all."""
    pool, _, _ = pool_for(settings, authorized=False)

    with pytest.raises(sessions.SessionError) as caught:
        await pool.get(USER)

    assert caught.value.expired
    assert not pool.has_session(USER)


async def test_a_flood_wait_is_reported_with_its_seconds(settings, stored) -> None:
    """E14 for the login path; the same answer for a reconnect."""
    pool, _, _ = pool_for(settings, connect_error=errors.FloodWait(value=42))

    with pytest.raises(sessions.SessionError, match="42 seconds"):
        await pool.get(USER)


# ── the live cap and LRU eviction ───────────────────────────────────────


async def test_reaching_the_cap_evicts_the_least_recently_used_idle_client(
    settings, stored
) -> None:
    pool, factory, clock = pool_for(settings)
    for user_id in (1, 2, 3):
        (paths.SESSIONS_DIR / f"{user_id}.session").write_bytes(b"sqlite")

    await pool.get(1)
    clock.advance(1)
    await pool.get(2)
    clock.advance(1)
    await pool.get(1)  # 1 is used again, so 2 becomes the least recently used
    clock.advance(1)
    await pool.get(3)

    assert factory.clients[2].closed, "the idle, least-recently-used client is the one evicted"
    assert not factory.clients[1].closed
    assert pool.live_count == 2


async def test_the_cap_refuses_only_when_every_live_client_is_busy(settings, stored) -> None:
    """Closing a client mid-fetch would fail the download it is in the middle of (R3)."""
    pool, factory, _ = pool_for(settings)
    for user_id in (1, 2, 3):
        (paths.SESSIONS_DIR / f"{user_id}.session").write_bytes(b"sqlite")

    async with pool.borrow(1), pool.borrow(2):
        with pytest.raises(sessions.SessionError, match="busy"):
            await pool.get(3)

    assert not factory.clients[1].closed and not factory.clients[2].closed
    assert pool.live_count == 2

    # With both released, the same request succeeds by evicting one of them.
    await pool.get(3)
    assert pool.live_count == 2


async def test_borrowing_releases_the_client_for_eviction_afterwards(settings, stored) -> None:
    pool, factory, clock = pool_for(settings)
    for user_id in (1, 2, 3):
        (paths.SESSIONS_DIR / f"{user_id}.session").write_bytes(b"sqlite")

    async with pool.borrow(1):
        pass
    clock.advance(1)
    await pool.get(2)
    clock.advance(1)
    await pool.get(1)  # touch 1 so 2 is the candidate
    clock.advance(1)
    await pool.get(3)

    assert factory.clients[2].closed


# ── idle reaping ────────────────────────────────────────────────────────


async def test_reaping_closes_an_idle_client_and_leaves_the_file(settings, stored) -> None:
    """The whole point of D7: the socket closes, the login survives it."""
    pool, factory, clock = pool_for(settings)
    await pool.get(USER)

    clock.advance(settings.runtime.session_idle_seconds + 1)
    reaped = await pool.reap()

    assert reaped == (USER,)
    assert factory.clients[USER].closed
    assert not pool.is_live(USER)
    assert pool.has_session(USER), "reaping must never delete a session file"


async def test_a_recently_used_client_is_left_alone(settings, stored) -> None:
    pool, _, clock = pool_for(settings)
    await pool.get(USER)

    clock.advance(settings.runtime.session_idle_seconds - 1)

    assert await pool.reap() == ()
    assert pool.is_live(USER)


async def test_a_busy_client_is_not_reaped(settings, stored) -> None:
    pool, _, clock = pool_for(settings)

    async with pool.borrow(USER):
        clock.advance(settings.runtime.session_idle_seconds * 10)
        assert await pool.reap() == ()

    assert pool.is_live(USER)


async def test_reaping_reports_every_user_it_closed(settings, stored) -> None:
    pool, _, clock = pool_for(settings)
    for user_id in (1, 2):
        (paths.SESSIONS_DIR / f"{user_id}.session").write_bytes(b"sqlite")
        await pool.get(user_id)

    clock.advance(settings.runtime.session_idle_seconds + 1)

    assert set(await pool.reap()) == {1, 2}
    assert pool.live_count == 0


# ── logging in and out ──────────────────────────────────────────────────


async def test_adopting_a_logged_in_client_makes_it_live(settings, stored) -> None:
    """The login flow connects its own client; the pool takes over from there."""
    pool, _, _ = pool_for(settings)
    client = FakeUserClient()
    await client.connect()

    await pool.add(USER, client)

    assert pool.is_live(USER)
    assert await pool.get(USER) is client


async def test_forget_disconnects_and_deletes(settings, stored) -> None:
    pool, factory, _ = pool_for(settings)
    await pool.get(USER)

    removed = await pool.forget(USER)

    assert removed
    assert factory.clients[USER].closed
    assert not pool.has_session(USER)
    assert pool.badge(USER) == (False, False)


async def test_forget_says_so_when_there_was_nothing(settings, stored) -> None:
    """FR-13's logout: "If none existed, say so neutrally" — so this must be answerable."""
    pool, _, _ = pool_for(settings)

    assert await pool.forget(USER + 1) is False


async def test_forget_removes_the_journal_a_crash_can_leave_beside_the_database(
    settings, stored
) -> None:
    pool, _, _ = pool_for(settings)
    journal = stored / f"{USER}.session-journal"
    journal.write_bytes(b"partial transaction")

    assert await pool.forget(USER)

    assert not journal.exists(), "a stale journal would be read by the next login"


async def test_closing_everything_leaves_no_open_client(settings, stored) -> None:
    pool, factory, _ = pool_for(settings)
    for user_id in (1, 2):
        (paths.SESSIONS_DIR / f"{user_id}.session").write_bytes(b"sqlite")
        await pool.get(user_id)

    await pool.close_all()

    assert pool.live_count == 0
    assert factory.clients[1].closed and factory.clients[2].closed


# ── the client the pool builds ──────────────────────────────────────────


def test_the_pool_builds_a_client_on_the_users_session_file(settings) -> None:
    """The library composes ``workdir / (name + '.session')``, so both halves are asserted."""
    client = sessions.build_client(settings, USER)

    assert client.name == str(USER)
    assert Path(client.workdir).resolve() == paths.SESSIONS_DIR.resolve()
    assert paths.session_file(USER) == paths.SESSIONS_DIR / f"{USER}.session"


def test_a_test_mode_login_uses_its_own_directory(settings, monkeypatch) -> None:
    """A live test must never be able to overwrite a real user's session."""
    test_settings = settings.model_copy(update={"test_mode": True})

    client = sessions.build_client(test_settings, USER)

    assert Path(client.workdir).resolve() == paths.TEST_SESSIONS_DIR.resolve()


def test_the_pool_lookup_is_injectable(settings) -> None:
    """Handlers reach the pool through a module-level lookup; a test must be able to replace it."""
    pool = sessions.SessionPool(settings)

    sessions.set_pool(pool)
    try:
        assert sessions.get_pool() is pool
    finally:
        sessions.set_pool(None)


# ── what a live session listens for (FR-5, FR-6) ────────────────────────


async def test_connecting_attaches_the_magic_word_and_auto_save(settings, stored) -> None:
    """Both react to the *user's* own messages, so they only exist on a live user client."""
    pool, factory, _ = pool_for(settings)

    await pool.get(USER)

    groups = [group for _handler, group in factory.clients[USER].handlers]
    assert groups == [magic.MAGIC_GROUP, timer.TIMER_GROUP]


async def test_the_features_are_attached_before_the_dispatcher_starts(settings, stored) -> None:
    """A handler added after ``initialize()`` would miss whatever arrived in between."""
    pool, factory, _ = pool_for(settings)

    await pool.get(USER)

    assert factory.clients[USER].handlers, "attached"
    assert factory.clients[USER].is_initialized, "and the dispatcher started after them"


async def test_a_fetch_only_pool_attaches_nothing(settings, stored) -> None:
    """The escape hatch the fetch-only paths (and their tests) rely on."""
    factory = FakeFactory()
    pool = sessions.SessionPool(settings, factory=factory, register_features=False)

    await pool.get(USER)

    assert factory.clients[USER].handlers == []


async def test_a_failed_connect_attaches_nothing(settings, stored) -> None:
    pool, factory, _ = pool_for(settings, me_error=errors.AuthKeyUnregistered())

    with pytest.raises(sessions.SessionError):
        await pool.get(USER)

    assert factory.clients[USER].handlers == []
