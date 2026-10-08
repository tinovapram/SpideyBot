"""Per-user submission limiting (D22, NFR-2).

A **token bucket** rather than a fixed "N per minute" counter. A fixed window lets a user spend
the whole allowance at 11:59:59 and the next one at 12:00:00 — double the intended burst through
a door the limiter was built to close. A bucket refills continuously, so the worst case is the
allowance itself, spread over a minute.

State is per process and per user. A bucket that has refilled to full is dropped, because it
means the same thing as no bucket — and since a check always leaves its own bucket partially
spent, :meth:`SubmissionLimiter.check` also sweeps idle buckets once the dictionary grows past a
threshold. That is what keeps the dictionary bounded by users active in the last minute rather
than by every user the bot has ever seen, without any caller having to run a sweep on a timer.

The clock is injected (D18) so a test can spend an allowance and watch it come back without
sleeping through the refill.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from app.config import Settings

SECONDS_PER_MINUTE = 60.0

#: Above this many tracked users, :meth:`SubmissionLimiter.check` sweeps the idle ones out. The
#: sweep is amortized this way so the bound holds without a caller having to run it on a timer.
DEFAULT_PRUNE_THRESHOLD = 1024


def _utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class Decision:
    """The limiter's answer, and what to tell the user when it is no."""

    allowed: bool
    retry_after: float = 0.0
    remaining: float = 0.0


@dataclass
class _Bucket:
    tokens: float
    updated_at: datetime


class SubmissionLimiter:
    """One token bucket per user, refilled at ``runtime.rate_limit_per_minute`` per minute."""

    def __init__(
        self,
        settings: Settings,
        *,
        clock: Callable[[], datetime] = _utcnow,
        prune_threshold: int = DEFAULT_PRUNE_THRESHOLD,
    ) -> None:
        self._rate = float(settings.runtime.rate_limit_per_minute)
        self._clock = clock
        self._prune_threshold = prune_threshold
        self._buckets: dict[int, _Bucket] = {}

    @property
    def rate_per_minute(self) -> float:
        return self._rate

    @property
    def tracked(self) -> int:
        """How many users currently have a partially-spent allowance."""
        return len(self._buckets)

    def check(self, user_id: int, *, now: datetime | None = None) -> Decision:
        """Spend one token for this user, or refuse and say how long to wait.

        Refusing does not spend anything, so a user who retries immediately does not lose the
        allowance they are waiting for.
        """
        moment = now or self._clock()
        if len(self._buckets) > self._prune_threshold:
            self.prune(now=moment)

        bucket = self._refill(user_id, moment)

        if bucket.tokens < 1.0:
            missing = 1.0 - bucket.tokens
            self._buckets[user_id] = bucket
            return Decision(allowed=False, retry_after=missing / self._per_second(), remaining=0.0)

        bucket.tokens -= 1.0
        self._buckets[user_id] = bucket
        return Decision(allowed=True, remaining=bucket.tokens)

    def prune(self, *, now: datetime | None = None) -> int:
        """Drop every bucket that has refilled to full, and report how many went.

        A full bucket means the same thing as no bucket, and a check leaves its own bucket
        partially spent — so without this, a user who submitted once and never came back would be
        remembered forever. Called from :meth:`check` once the dictionary grows past the
        threshold, so the bound holds without any external caller having to remember to sweep.
        """
        moment = now or self._clock()
        spent = [
            user_id for user_id, bucket in self._buckets.items() if self._full_at(bucket, moment)
        ]
        for user_id in spent:
            del self._buckets[user_id]
        return len(spent)

    def forget(self, user_id: int) -> None:
        """Drop a user's bucket, so their next submission starts from a full allowance.

        Only for the user's own actions that reset the situation — not for a retry after a
        refusal, which would make the limiter trivial to sidestep.
        """
        self._buckets.pop(user_id, None)

    def _per_second(self) -> float:
        return self._rate / SECONDS_PER_MINUTE

    def _full_at(self, bucket: _Bucket, moment: datetime) -> bool:
        return bucket.tokens + self._elapsed(bucket, moment) * self._per_second() >= self._rate

    def _elapsed(self, bucket: _Bucket, moment: datetime) -> float:
        # A clock that went backwards (an NTP correction) must not mint allowance, so negative
        # elapsed time is treated as no time passing.
        return max(0.0, (moment - bucket.updated_at).total_seconds())

    def _refill(self, user_id: int, moment: datetime) -> _Bucket:
        bucket = self._buckets.get(user_id)
        if bucket is None:
            return _Bucket(tokens=self._rate, updated_at=moment)

        if self._full_at(bucket, moment):
            # Idle long enough to be back where it started, so there is nothing to remember.
            del self._buckets[user_id]
            return _Bucket(tokens=self._rate, updated_at=moment)

        bucket.tokens = min(
            self._rate, bucket.tokens + self._elapsed(bucket, moment) * self._per_second()
        )
        bucket.updated_at = moment
        return bucket
