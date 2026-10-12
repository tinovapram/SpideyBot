"""The pool of live user clients (D7, D10, D16, D21, FR-13).

A *user session* is a Kurigram client that speaks as the user. It is what lets the bot reach
private or restricted Telegram content, and it is expensive in a way a bot client is not: one
socket, one MTProto auth key, and one entry in the user's own *Active Sessions* list. So the pool
is not a cache with a nice LRU — it is a budget with three separate limits, and every one of them
answers a different way of running out:

* the **live cap** (``runtime.session_max_live``) bounds concurrent sockets, and it is enforced
  when a client is asked for, not when it is created: reaching the cap evicts the
  least-recently-used *idle* client, and if every live client is busy the request is refused
  rather than served by disconnecting somebody's download (R3).
* the **idle timeout** (``runtime.session_idle_seconds``) bounds how long a socket stays open for
  a user who has stopped asking for anything. Reaping disconnects; it never deletes — a session
  file outliving its client is the whole point of D7, and it is what makes a restart reconnect
  instead of logging in again.
* **one session per user** (FR-13) is structural: the map is keyed by user id, and the session
  file is named by it, so a second login for the same user would overwrite the first.

Two behaviours are worth stating because they are not obvious from the signatures:

* **A connection is proved, not assumed.** ``connect()`` returning without raising is not evidence
  that the auth key is still authorized — a revoked key is often only discovered by *making* a
  request — so every reconnect asks ``get_me()`` and treats a failure as the session being dead.
  That is the same eager-validation rule the Reddit adapter follows (D46), for the same reason: a
  client that cannot work should fail where it is opened, not halfway through a user's download.
* **A dead session file is deleted** (the ``expired`` flag on :class:`SessionError`). The spec's
  session states are `🟢 Connected` / `🟡 Session saved (not running)` / `🔴 No session`, and a file
  whose auth key Telegram has revoked is none of those — keeping it would make ``/account`` claim a
  session that can never connect. Deleting it is also what ``/logout`` does (D10), so the state
  converges on the same place either way.
"""

from __future__ import annotations

import contextlib
import logging
import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pyrogram import errors

from app import paths
from app.config import Settings
from telegram import clients

log = logging.getLogger(__name__)

#: Telegram's answer for "this auth key is not usable any more", in all the spellings the library
#: gives it. Membership is checked against the class, not the string, because the names are the
#: library's and a message would be ours.
DEAD_SESSION_ERRORS: tuple[type[Exception], ...] = tuple(
    error
    for name in (
        "AuthKeyUnregistered",
        "AuthKeyInvalid",
        "AuthKeyDuplicated",
        "SessionRevoked",
        "SessionExpired",
        "UserDeactivated",
        "UserDeactivatedBan",
    )
    if isinstance((error := getattr(errors, name, None)), type)
)


class SessionError(RuntimeError):
    """A session operation that failed for a reason the user can be told about.

    ``reason`` is user-facing copy, ``detail`` is for the log, and ``expired`` says the stored
    file is gone — which is the difference between "try again" and "log in again".
    """

    def __init__(self, reason: str, *, detail: str = "", expired: bool = False) -> None:
        # ``str()`` is the user-facing reason on purpose: this exception's text is shown in a chat,
        # so a caller (and a test) matching on it should be matching what the user reads. The
        # diagnostic detail rides alongside for the log rather than replacing it.
        super().__init__(reason)
        self.reason = reason
        self.detail = detail
        self.expired = expired


@dataclass
class _Live:
    """One connected client, and what the pool needs to know about it."""

    client: Any
    last_used: float
    #: True while a caller is inside :meth:`SessionPool.borrow`. A busy client is never evicted:
    #: closing it would fail the download it is in the middle of, which is a worse outcome than
    #: refusing a new request.
    busy: bool = False
    #: Everything this client fetched, so reaping can report what it cost.
    uses: int = field(default=0)


def build_client(settings: Settings, user_id: int) -> Any:
    """A not-yet-connected client for one user, backed by that user's session file.

    ``name`` is the bare user id and ``workdir`` is the sessions directory because that is how the
    library composes the path (``workdir / (name + ".session")``); passing a full path as ``name``
    would work by accident and break the moment the workdir changes. ``no_updates`` is on: this
    client exists to *fetch*, and a second dispatcher competing for the same account's updates
    would be a bug waiting to happen (the magic word and TimerMedia read their own way — P6).
    """
    return clients.build_user(settings, user_id)


class SessionPool:
    """Live user clients, bounded by the cap and the idle timeout (D21)."""

    def __init__(
        self,
        settings: Settings,
        *,
        factory: Callable[[int], Any] | None = None,
        clock: Callable[[], float] = time.monotonic,
        register_features: bool = True,
    ) -> None:
        self._settings = settings
        self._factory = factory or (lambda user_id: build_client(settings, user_id))
        self._clock = clock
        self._register_features = register_features
        self._live: dict[int, _Live] = {}

    # ── what the handlers and the status copy ask ────────────────────────

    def path(self, user_id: int) -> Path:
        """Where this user's session file lives (without the suffix the library appends)."""
        return paths.session_path(user_id, test_mode=self._settings.test_mode)

    def file(self, user_id: int) -> Path:
        """The file itself, for existence checks."""
        return paths.session_file(user_id, test_mode=self._settings.test_mode)

    def has_session(self, user_id: int) -> bool:
        """Whether a session is *stored*. Says nothing about whether it still works."""
        return self.file(user_id).is_file()

    def is_live(self, user_id: int) -> bool:
        """Whether a client for this user is connected right now."""
        live = self._live.get(user_id)
        return live is not None and bool(getattr(live.client, "is_connected", True))

    @property
    def live_count(self) -> int:
        return len(self._live)

    def live_ids(self) -> tuple[int, ...]:
        """Live users, least-recently-used first — the order eviction considers them in."""
        return tuple(
            user_id
            for user_id, _ in sorted(self._live.items(), key=lambda item: item[1].last_used)
        )

    def badge(self, user_id: int) -> tuple[bool, bool]:
        """``(has_session, is_live)`` — the two facts the session badge is built from."""
        return self.has_session(user_id), self.is_live(user_id)

    #: True when a *connect* should register the handlers that make a session useful (FR-5, FR-6):
    #: the magic word and auto-save react to the user's own messages, so they live on this client
    #: rather than on the bot's. Off only for a client that fetches and nothing else, which is what
    #: the tests build.
    register_features: bool = True

    # ── connecting and disconnecting ─────────────────────────────────────

    def new_client(self, user_id: int) -> Any:
        """A fresh, **unconnected** client for this user.

        The login flow needs one before the pool would ever accept it: it connects, sends a code
        and signs in, and only then does :meth:`add` make it the live client. Handing the same
        factory to both is what guarantees the login client and the later pooled client are the same
        session file — a second `Client(...)` built some other way would be a second session.
        """
        return self._factory(user_id)

    async def get(self, user_id: int) -> Any:
        """The user's connected client, connecting from the stored file if needed.

        Raises :class:`SessionError` when there is no session, when the stored one is dead, or when
        the pool is at its cap with every live client busy.
        """
        live = self._live.get(user_id)
        if live is not None:
            live.last_used = self._clock()
            return live.client

        if not self.has_session(user_id):
            raise SessionError("No session stored for this user. Use /login first.")

        await self._make_room(user_id)
        client = self._factory(user_id)
        try:
            # ``connect()`` answers whether the stored session is *authorized*, which is the first
            # time a revoked key can be noticed without making a request. It is not the whole
            # lifecycle: ``initialize()`` is what starts the workers/plugins a client needs before
            # it can be used for anything, and ``start()`` — the library's own path — is these steps
            # plus the interactive authorization we must never reach for an existing session.
            authorized = await client.connect()
            if not authorized:
                await self._discard(client)
                self.forget_file(user_id)
                raise SessionError(
                    "The stored session is no longer valid. Use /login to connect again.",
                    detail="connect() reported the session as unauthorized",
                    expired=True,
                )
            # A revoked key can also connect and then refuse to answer, so the proof is a request.
            me = await client.get_me()
            if me is None:
                await self._discard(client)
                self.forget_file(user_id)
                raise SessionError(
                    "The stored session is no longer valid. Use /login to connect again.",
                    detail="get_me() returned nothing",
                    expired=True,
                )
            if self._register_features:
                self._attach_features(client, user_id)
            await client.initialize()
        except errors.FloodWait as wait:
            await self._discard(client)
            raise SessionError(
                f"Telegram is rate limiting this login. Try again in {wait.value} seconds.",
                detail=f"flood wait {wait.value}s while connecting",
            ) from wait
        except DEAD_SESSION_ERRORS as dead:
            await self._discard(client)
            self.forget_file(user_id)
            raise SessionError(
                "The stored session is no longer valid. Use /login to connect again.",
                detail=f"{type(dead).__name__}: {dead}",
                expired=True,
            ) from dead
        except SessionError:
            raise
        except Exception as unreachable:
            await self._discard(client)
            raise SessionError(
                "Could not connect to Telegram with the stored session. Try again shortly.",
                detail=f"{type(unreachable).__name__}: {unreachable}",
            ) from unreachable

        self._live[user_id] = _Live(client=client, last_used=self._clock())
        log.info("session connected", extra={"user_id": user_id, "live": len(self._live)})
        return client

    @asynccontextmanager
    async def borrow(self, user_id: int) -> AsyncIterator[Any]:
        """The user's client, marked busy for the duration so reaping leaves it alone.

        A *fetch* is the caller that needs this: a client evicted halfway through downloading a
        user's album would surface as a mid-download failure with no cause an operator could see.
        """
        client = await self.get(user_id)
        live = self._live.get(user_id)
        if live is not None:
            live.busy = True
            live.uses += 1
        try:
            yield client
        finally:
            if live is not None:
                live.busy = False
                live.last_used = self._clock()

    async def add(self, user_id: int, client: Any) -> None:
        """Adopt a client that a login flow has already connected and signed in.

        The pool takes ownership of its lifetime: from here it is evicted, reaped and closed like
        any other, and the login flow must not disconnect it afterwards.
        """
        existing = self._live.get(user_id)
        if existing is not None and existing.client is not client:
            await self._discard(existing.client)
        self._live[user_id] = _Live(client=client, last_used=self._clock())

    async def forget(self, user_id: int) -> bool:
        """Disconnect this user and delete their session file. Returns whether anything was there.

        This is the local half of ``/logout``; revoking the key server-side is the caller's step,
        because it is a request that can fail on its own (D10).
        """
        live = self._live.pop(user_id, None)
        if live is not None:
            await self._discard(live.client)
        return self.forget_file(user_id)

    def forget_file(self, user_id: int) -> bool:
        """Delete the session file without touching a live client.

        The library keeps its session in SQLite, which can leave a journal beside the database
        after a crash — removing only ``.session`` would leave the next login reading a stale
        transaction.
        """
        removed = False
        base = self.file(user_id)
        journal = base.with_name(base.name + "-journal")
        write_ahead = base.with_name(base.name + "-wal")
        for path in (base, journal, write_ahead):
            if path.is_file():
                path.unlink()
                removed = True
        return removed

    async def reap(self, *, now: float | None = None) -> tuple[int, ...]:
        """Disconnect users who have asked for nothing for too long. Returns their ids (D21)."""
        moment = self._clock() if now is None else now
        idle = self._settings.runtime.session_idle_seconds
        reaped: list[int] = []
        for user_id, live in list(self._live.items()):
            if live.busy or moment - live.last_used < idle:
                continue
            if self._live.pop(user_id, None) is not None:
                await self._discard(live.client)
                reaped.append(user_id)
                log.info(
                    "session reaped",
                    extra={"user_id": user_id, "idle": round(moment - live.last_used, 1)},
                )
        return tuple(reaped)

    async def close_all(self) -> None:
        """Disconnect every live client. Called at shutdown, so nothing is left half-open."""
        for user_id in list(self._live):
            live = self._live.pop(user_id, None)
            if live is not None:
                await self._discard(live.client)

    # ── internals ────────────────────────────────────────────────────────

    def _attach_features(self, client: Any, user_id: int) -> None:
        """Register the two things a live session does on its own (FR-5, FR-6).

        Before ``initialize()``, because that is what starts the dispatcher: a handler added
        afterwards would miss whatever arrives in between. Imported here rather than at module scope
        because both modules read the database, so importing them at the top would make
        ``telegram.sessions`` need ``app.db`` to be configured merely to be imported — exactly the
        coupling that makes a unit test require a Postgres.
        """
        from telegram import magic, timer

        magic.register(client, self._settings, user_id=user_id)
        timer.register(client, self._settings, user_id=user_id)

    async def _make_room(self, user_id: int) -> None:
        """Evict idle clients until one more may be connected, or refuse (R3)."""
        cap = self._settings.runtime.session_max_live
        while len(self._live) >= cap:
            candidate = next(
                (uid for uid in self.live_ids() if not self._live[uid].busy),
                None,
            )
            if candidate is None:
                raise SessionError(
                    "Every session slot is busy right now. Try again in a moment.",
                    detail=f"{cap} live sessions, all in use",
                )
            live = self._live.pop(candidate)
            await self._discard(live.client)
            log.info("session evicted for capacity", extra={"user_id": candidate, "cap": cap})

    @staticmethod
    async def _discard(client: Any) -> None:
        """Close a client, ignoring the ways closing a broken connection can fail.

        ``stop()`` rather than ``disconnect()``: the library refuses to disconnect an *initialized*
        client, and a connect that failed after start-up is exactly that. ``stop()`` is terminate
        plus disconnect, and it raises only when there was nothing to stop — which is the case that
        must not turn a real error into a different one.
        """
        with contextlib.suppress(Exception):
            await client.stop()


def _pool(settings: Settings | None = None) -> SessionPool:
    """The process-wide pool.

    Built on first use like :func:`app.config.get_settings`, and injectable, because handlers reach
    it the same way they reach settings — a module-level lookup that a test can replace.
    """
    global _POOL
    if _POOL is None:
        from app import config

        _POOL = SessionPool(settings or config.get_settings())
    return _POOL


def get_pool(settings: Settings | None = None) -> SessionPool:
    """The pool the handlers use."""
    return _pool(settings)


def set_pool(pool: SessionPool | None) -> None:
    """Install a pool (tests), or clear it so the next call builds a fresh one."""
    global _POOL
    _POOL = pool


_POOL: SessionPool | None = None
