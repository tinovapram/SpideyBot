"""
Paced media preparation and sends — one gate per Telethon client.

Telegram's flood limits apply per account, so the bot client and every user
session get their own :class:`SendGate`. The limited unit is **one prepared
media** (one file the user asked for), whether it is later delivered inside an
album or on its own:

* ``media_slot()`` bounds how many media are in flight (heavy files), and
* ``take_media()`` bounds how many start per minute (many small files).

Sends are paced per chat only, because every media send is already covered by a
media token. A flood on one account cannot stall another.

Uploads go through :meth:`SendGate.media_slot` / :meth:`SendGate.take_media` and
sends through :meth:`SendGate.send`, so all flows (``/dl``, ``/dt``, TeraBox,
magic word) inherit the pacing. ``docs/SEND_RATE_LIMITING.md`` explains it.
"""

from __future__ import annotations

import asyncio
import contextlib
import random
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any, TypeVar

import structlog
from telethon.errors import (
    FilePartMissingError,
    FileReferenceExpiredError,
    FloodWaitError,
    RPCError,
    SlowModeWaitError,
)

from core.config import get_settings
from core.metrics import get_metrics

logger = structlog.get_logger(__name__)

T = TypeVar("T")

Sleep = Callable[[float], Awaitable[None]]

_TRANSIENT_BACKOFF_BASE = 2.0
_TRANSIENT_BACKOFF_MAX = 30.0
_LONG_FLOOD_SECONDS = 30.0
_IDLE_BUCKET_SECONDS = 1800.0

_media_total_in_flight = 0


class FileExpiredError(RuntimeError):
    """The uploaded file reference expired, so the file has to be re-uploaded."""


class TokenBucket:
    """Continuous-refill token bucket.

    ``per_minute`` tokens refill evenly, so once the bucket is empty a caller
    never waits longer than ``60 / per_minute`` seconds. ``burst`` caps how many
    tokens may be spent in the same instant — a full bucket lets that many
    through immediately and no more.
    """

    def __init__(
        self,
        per_minute: int,
        *,
        burst: int | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        self.per_minute = max(1, int(per_minute))
        self.capacity = float(max(1, int(burst)) if burst else self.per_minute)
        self._rate = self.per_minute / 60.0
        self._tokens = self.capacity
        self._clock = clock
        self._sleep = sleep
        self._updated = clock()
        self.last_used = self._updated

    def _refill(self, now: float) -> None:
        elapsed = max(0.0, now - self._updated)
        if elapsed:
            self._tokens = min(self.capacity, self._tokens + elapsed * self._rate)
            self._updated = now

    async def take(self) -> float:
        """Consume one token, waiting when empty. Returns the seconds waited."""
        waited = 0.0
        while True:
            now = self._clock()
            self._refill(now)
            self.last_used = now
            if self._tokens >= 1.0:
                self._tokens -= 1.0
                return waited
            delay = (1.0 - self._tokens) / self._rate
            waited += delay
            await self._sleep(delay)


class SendGate:
    """Admission control for one client's uploads and sends."""

    def __init__(
        self,
        label: str,
        *,
        media_slots: int,
        media_per_minute: int,
        media_burst: int,
        send_chat_per_minute: int,
        send_burst: int,
        jitter_seconds: float,
        max_attempts: int,
        client: Any | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        self.label = label
        self.client = client
        self._per_chat_per_minute = max(1, send_chat_per_minute)
        self._send_burst = max(1, send_burst)
        self._jitter_seconds = max(0.0, jitter_seconds)
        self._max_attempts = max(1, max_attempts)
        self._clock = clock
        self._sleep = sleep

        self._media_bucket = TokenBucket(
            media_per_minute, burst=media_burst, clock=clock, sleep=sleep
        )
        self._chat_buckets: dict[Any, TokenBucket] = {}

        self._blocked_until = 0.0
        self._unblocked = asyncio.Event()
        self._unblocked.set()
        self._wake: asyncio.Task[None] | None = None

        self._media_limit = max(1, media_slots)
        self._media_in_flight = 0
        self._media_cond = asyncio.Condition()

    # ── State ────────────────────────────────────────────────────

    @property
    def blocked_for(self) -> float:
        """Remaining shared cooldown in seconds."""
        return max(0.0, self._blocked_until - self._clock())

    @property
    def media_in_flight(self) -> int:
        """Media currently being prepared on this client."""
        return self._media_in_flight

    @property
    def media_limit(self) -> int:
        """Current media-slot budget."""
        return self._media_limit

    async def set_media_slots(self, slots: int) -> None:
        """Update the media budget (user clients follow their tier)."""
        async with self._media_cond:
            self._media_limit = max(1, slots)
            self._media_cond.notify_all()

    def close(self) -> None:
        """Cancel the pending cooldown wake-up and release waiters."""
        if self._wake is not None and not self._wake.done():
            self._wake.cancel()
        self._wake = None
        self._blocked_until = 0.0
        self._unblocked.set()

    def prune_buckets(self, idle_seconds: float = _IDLE_BUCKET_SECONDS) -> int:
        """Forget per-chat buckets untouched for *idle_seconds*."""
        cutoff = self._clock() - idle_seconds
        stale = [chat for chat, bucket in self._chat_buckets.items() if bucket.last_used < cutoff]
        for chat in stale:
            del self._chat_buckets[chat]
        return len(stale)

    # ── Cooldown ─────────────────────────────────────────────────

    def _block(self, seconds: float) -> None:
        """Start — or extend — a cooldown shared by every waiter on this client."""
        delay = max(0.0, float(seconds)) + random.uniform(0.0, self._jitter_seconds)
        until = self._clock() + delay
        if until <= self._blocked_until:
            return
        self._blocked_until = until
        self._unblocked.clear()
        if self._wake is not None and not self._wake.done():
            self._wake.cancel()
        self._wake = asyncio.create_task(
            self._release_block(delay), name=f"sendgate-{self.label}"
        )
        log = logger.warning if delay >= _LONG_FLOOD_SECONDS else logger.info
        log("Telegram send cooldown", client=self.label, seconds=round(delay, 1))

    async def _release_block(self, delay: float) -> None:
        await self._sleep(delay)
        self._unblocked.set()
        logger.info("Send cooldown expired", client=self.label)

    async def _wait_unblocked(self) -> None:
        """Wait for the shared cooldown, waking once for all waiters."""
        while True:
            remaining = self._blocked_until - self._clock()
            if remaining <= 0:
                return
            try:
                await asyncio.wait_for(self._unblocked.wait(), timeout=remaining)
            except TimeoutError:
                pass

    # ── Admission ────────────────────────────────────────────────

    def _chat_bucket(self, chat_id: Any) -> TokenBucket:
        bucket = self._chat_buckets.get(chat_id)
        if bucket is None:
            bucket = TokenBucket(
                self._per_chat_per_minute,
                burst=self._send_burst,
                clock=self._clock,
                sleep=self._sleep,
            )
            self._chat_buckets[chat_id] = bucket
        return bucket

    async def _admit(self, chat_id: Any) -> float:
        started = self._clock()
        await self._wait_unblocked()
        await self._chat_bucket(chat_id).take()
        return self._clock() - started

    def _backoff(self, attempt: int) -> float:
        base = _TRANSIENT_BACKOFF_BASE * (2 ** max(0, attempt - 1))
        return min(base, _TRANSIENT_BACKOFF_MAX) + random.uniform(0.0, self._jitter_seconds)

    async def send(self, chat_id: Any, op: Callable[[], Awaitable[T]]) -> T:
        """Run ``op()`` paced by this gate, retrying Telegram's retryable errors.

        One token is spent per **message RPC** — an album of ten media costs the
        same as a single document. ``op`` must build a fresh awaitable on every
        call so a retry re-sends the already-uploaded media instead of
        transferring the file again.
        """
        attempts = 0
        while True:
            attempts += 1
            waited = await self._admit(chat_id)
            if waited > 0:
                get_metrics().add("send_gate_wait_seconds", waited)
            try:
                result = await op()
                get_metrics().incr("messages_sent")
                return result
            except (FloodWaitError, SlowModeWaitError) as exc:
                # The next loop's _admit() waits out the shared cooldown.
                self._block(exc.seconds)
                get_metrics().incr("send_flood_waits")
                get_metrics().incr_by("send_flood_waits_by_client", self.label)
                if attempts >= self._max_attempts:
                    logger.warning(
                        "Send failed after flood waits",
                        client=self.label,
                        attempts=attempts,
                    )
                    raise
                get_metrics().incr_by("send_retries_by_reason", "flood")
            except (FileReferenceExpiredError, FilePartMissingError) as exc:
                get_metrics().incr_by("send_retries_by_reason", "expired")
                logger.warning("Uploaded media expired", client=self.label, error=str(exc))
                raise FileExpiredError(
                    "The uploaded media expired on Telegram's side and cannot be re-sent."
                ) from exc
            except TimeoutError:
                if attempts >= self._max_attempts:
                    raise
                await self._retry_transient(attempts)
            except RPCError as exc:
                if exc.code is None or exc.code < 500 or attempts >= self._max_attempts:
                    raise
                await self._retry_transient(attempts)

    async def _retry_transient(self, attempt: int) -> None:
        """Back off before retrying a transient failure."""
        delay = self._backoff(attempt)
        get_metrics().incr("send_retries")
        get_metrics().incr_by("send_retries_by_reason", "transient")
        logger.warning("Send failed, retrying", client=self.label, delay=round(delay, 1))
        await self._sleep(delay)

    async def edit(self, op: Callable[[], Awaitable[T]]) -> T:
        """Run ``op()`` under the shared cooldown, spending no send tokens."""
        attempts = 0
        while True:
            attempts += 1
            await self._wait_unblocked()
            try:
                return await op()
            except (FloodWaitError, SlowModeWaitError) as exc:
                self._block(exc.seconds)
                get_metrics().incr("send_flood_waits")
                get_metrics().incr_by("send_flood_waits_by_client", self.label)
                if attempts >= self._max_attempts:
                    raise

    # ── Media preparation ────────────────────────────────────────

    async def take_media(self) -> None:
        """Spend one media token — one file the user asked for."""
        waited = await self._media_bucket.take()
        get_metrics().incr("media_prepared")
        if waited > 0:
            get_metrics().add("media_wait_seconds", waited)

    @contextlib.asynccontextmanager
    async def media_slot(self) -> AsyncIterator[None]:
        """Hold one media slot for a whole preparation (file plus thumbnail)."""
        global _media_total_in_flight

        async with self._media_cond:
            await self._media_cond.wait_for(
                lambda: self._media_in_flight < self._media_limit
            )
            self._media_in_flight += 1
        _media_total_in_flight += 1
        get_metrics().set("media_in_flight", _media_total_in_flight)
        try:
            yield
        finally:
            async with self._media_cond:
                self._media_in_flight -= 1
                self._media_cond.notify()
            _media_total_in_flight -= 1
            get_metrics().set("media_in_flight", _media_total_in_flight)


# ── Registry ─────────────────────────────────────────────────────

_gates: dict[int, SendGate] = {}


def label_client(client: Any, label: str) -> None:
    """Tag *client* so its gate and metrics can be told apart."""
    with contextlib.suppress(Exception):
        client._spidey_label = label


def _build_gate(client: Any | None) -> SendGate:
    settings = get_settings()
    label = getattr(client, "_spidey_label", None) or f"client:{id(client) & 0xFFFF}"
    return SendGate(
        str(label),
        media_slots=settings.media_slot_budget(),
        media_per_minute=settings.media_per_minute,
        media_burst=settings.media_burst,
        send_chat_per_minute=settings.send_chat_per_minute,
        send_burst=settings.send_burst,
        jitter_seconds=settings.flood_jitter_seconds,
        max_attempts=settings.send_max_attempts,
        client=client,
    )


def get_gate(client: Any | None) -> SendGate:
    """Return the gate for *client*, creating it on first use."""
    key = id(client) if client is not None else 0
    gate = _gates.get(key)
    if gate is None:
        gate = _build_gate(client)
        _gates[key] = gate
    return gate


def sweep(idle_seconds: float = _IDLE_BUCKET_SECONDS) -> int:
    """Drop gates of disconnected clients and idle per-chat buckets."""
    removed = 0
    for key, gate in list(_gates.items()):
        is_connected = getattr(gate.client, "is_connected", None)
        if callable(is_connected) and not is_connected():
            gate.close()
            del _gates[key]
            removed += 1
            continue
        gate.prune_buckets(idle_seconds)
    return removed


def reset() -> None:
    """Forget every gate (shutdown and tests)."""
    global _media_total_in_flight

    for gate in _gates.values():
        gate.close()
    _gates.clear()
    _media_total_in_flight = 0
    get_metrics().set("media_in_flight", 0)
