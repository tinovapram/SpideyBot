"""Worker slots: claim a job, run it, record the result (FR-10.1, FR-10.3, FR-11.6).

The pool is deliberately thin. It holds ``max_concurrent_downloads`` tasks, each of which
claims one job, runs it, and closes it out. Which *tier* may start at any moment is decided
entirely by the claiming statement in :mod:`app.queue`, so per-tier concurrency cannot drift
out of sync with the queue as the number of workers changes — there is one place that knows
the policy.

Running a job is not wrapped in the transaction that claimed it. The claim commits first,
because holding a transaction open for the length of a download would pin a connection and
keep a row lock for minutes; a lease is what makes the job safe without that. The lease is
then kept alive by a renewal task on a connection of its own, since the job's own session is
busy with the transfer and an ``AsyncSession`` cannot be shared by two tasks.

Handlers therefore open their own sessions when they need the database, rather than being
handed one. That keeps the "one session, one task at a time" rule impossible to violate by
accident, at the cost of a connection per unit of work — which is what the pool is sized for.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from sqlalchemy import select

from app import db as db_module
from app import queue
from app.config import Settings
from app.models import Job

log = logging.getLogger(__name__)

#: How long a worker waits before looking for work again. Short enough that a job submitted
#: while the queue was empty starts promptly, long enough not to hammer the database.
DEFAULT_IDLE_SLEEP = 1.0

#: A lease is renewed at a third of its length, so two renewals may be missed before it
#: expires — one lost packet or one slow query must not cost a job its lease.
RENEWAL_FRACTION = 3.0
MIN_RENEWAL_INTERVAL = 1.0


@dataclass(frozen=True)
class JobOutcome:
    """What running a job produced — the handler's only channel back to the queue."""

    ok: bool
    reason: str | None = None
    #: Whether a failure is worth another attempt. False for a decision that cannot change: a link
    #: over the tier's limit stays over it (AC-3), and a share with no media stays empty (E4).
    #: Retrying those would spend two more attempts, two more downloads from the allowance and two
    #: more messages to tell the user the same thing.
    retry: bool = True


def failure(reason: str) -> JobOutcome:
    """A run that did not deliver, with something worth telling the user or the log."""
    return JobOutcome(ok=False, reason=reason)


JobHandler = Callable[[Job, "LeaseKeeper"], Awaitable[JobOutcome]]


class LeaseKeeper:
    """Keeps a running job's lease alive, and surfaces a cancellation request.

    ``stop_requested`` is how FR-11.6 is actually achieved. The database cannot interrupt a
    transfer, so the request is written by ``/cancel`` and read here, and the handler is
    expected to check for it between files (or mid-transfer) and return early. Renewal goes
    on until the handler returns, so a job that is winding down still cannot be taken over.
    """

    def __init__(self, job: Job, settings: Settings, *, interval: float | None = None) -> None:
        self._job = job
        self._settings = settings
        seconds = (
            interval
            if interval is not None
            else settings.runtime.job_lease_seconds / RENEWAL_FRACTION
        )
        self._interval = max(MIN_RENEWAL_INTERVAL, seconds)
        self._task: asyncio.Task[None] | None = None
        self._stop_requested = False
        self._lease_lost = False

    @property
    def stop_requested(self) -> bool:
        """Whether the user asked this job to stop."""
        return self._stop_requested

    @property
    def lease_lost(self) -> bool:
        """Whether another worker took this job over.

        Everything this run produces is then void: the other worker owns the job, and writing
        a terminal state here would overwrite its outcome with a stale one.
        """
        return self._lease_lost

    async def __aenter__(self) -> LeaseKeeper:
        self._task = asyncio.create_task(self._watch(), name=f"lease-{self._job.id}")
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self._cancel_watch()

    async def _cancel_watch(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._task
        self._task = None

    async def _watch(self) -> None:
        while True:
            await asyncio.sleep(self._interval)
            async with db_module.session() as session:
                if await self._cancellation_requested(session):
                    self._stop_requested = True
                if not await queue.renew(session, self._job, self._settings):
                    self._lease_lost = True
                    log.warning("job %s lost its lease; abandoning this run", self._job.id)
                    return

    async def _cancellation_requested(self, session) -> bool:
        result = await session.execute(
            select(Job.cancel_requested_at).where(Job.id == self._job.id)
        )
        return result.scalar_one_or_none() is not None


async def run_once(settings: Settings, handler: JobHandler) -> Job | None:
    """Claim a single job and run it to a terminal state.

    Returns the job that ran, or ``None`` when there was nothing claimable — an idle queue, or
    every waiting job belonging to a user already at their slot count. Neither is an error.
    """
    async with db_module.session() as session:
        job = await queue.claim(session, settings)
    if job is None:
        return None
    return await execute(job.id, settings, handler)


async def execute(job_id: int, settings: Settings, handler: JobHandler) -> Job | None:
    """Run one already-claimed job and record what happened to it."""
    async with db_module.session() as session:
        job = await session.get(Job, job_id)
        if job is None or job.status != queue.RUNNING:
            # Cancelled or reclaimed between claim and here: the other decision stands.
            return job

    keeper = LeaseKeeper(job, settings)
    async with keeper:
        outcome = await _call(handler, job, keeper)

    async with db_module.session() as session:
        current = await session.get(Job, job_id)
        if current is None:
            return None

        if keeper.lease_lost:
            log.warning("discarding the result of job %s: it belongs to another worker", job_id)
            return current

        if keeper.stop_requested or current.cancel_requested_at is not None:
            # Checked before the outcome: a delivery that completed just as the user cancelled
            # is recorded as cancelled, because that is what the user asked for (FR-11.5).
            await queue.terminate(session, current, queue.CANCELLED)
        elif outcome.ok:
            await queue.mark_delivered(session, current)
        elif outcome.retry and current.attempts < settings.runtime.job_max_attempts:
            await queue.requeue(session, current)
        else:
            await queue.mark_failed(session, current)
        return current


async def _call(handler: JobHandler, job: Job, keeper: LeaseKeeper) -> JobOutcome:
    """Run the handler, turning anything unexpected into a failed outcome.

    A handler that raises must not take the worker down with it: one bad link would then stop
    every other user's downloads. Shutdown is the exception — that cancels the task so the
    lease can expire and the job can be picked up again after the restart (FR-10.5).
    """
    try:
        return await handler(job, keeper)
    except asyncio.CancelledError:
        raise
    except Exception as error:  # noqa: BLE001 - deliberately broad, see docstring
        log.exception("job %s raised out of its handler", job.id)
        return failure(str(error) or error.__class__.__name__)


class WorkerPool:
    """One task per slot, all pulling from the same queue.

    The pool size is the global cap; the per-tier limits are applied by the claim. Running
    more tasks than a tier allows is therefore harmless — the extra tasks find nothing
    claimable and idle — which is what lets the pool be sized once, at startup, instead of
    being rebuilt whenever a tier's configuration changes.
    """

    def __init__(
        self,
        settings: Settings,
        handler: JobHandler,
        *,
        idle_sleep: float = DEFAULT_IDLE_SLEEP,
        size: int | None = None,
    ) -> None:
        self._settings = settings
        self._handler = handler
        self._idle_sleep = idle_sleep
        self._size = size if size is not None else settings.runtime.max_concurrent_downloads
        self._tasks: list[asyncio.Task[None]] = []
        self._stopping = False

    @property
    def size(self) -> int:
        return self._size

    @property
    def running(self) -> bool:
        return bool(self._tasks)

    async def start(self) -> None:
        if self._tasks:
            return
        self._stopping = False
        self._tasks = [
            asyncio.create_task(self._loop(index), name=f"spideybot-worker-{index}")
            for index in range(self._size)
        ]

    async def stop(self) -> None:
        """Cancel every slot and wait for them to unwind.

        Slots are cancelled rather than drained: a shutdown that waited for a multi-gigabyte
        download to finish could take hours, and the lease already guarantees the job resumes.
        """
        self._stopping = True
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._tasks = []

    async def _loop(self, index: int) -> None:
        while not self._stopping:
            try:
                job = await run_once(self._settings, self._handler)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - a slot must survive a transient database error
                log.exception("worker %d hit an unexpected error; backing off", index)
                await asyncio.sleep(self._idle_sleep)
                continue

            if job is None:
                await asyncio.sleep(self._idle_sleep)
