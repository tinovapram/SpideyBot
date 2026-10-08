"""The per-user submission limiter (D22).

Pure logic with an injected clock, so these need no database and never wait for a refill.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.config import build_settings
from app.ratelimit import SubmissionLimiter

START = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)


class Clock:
    """A clock the test moves by hand, standing in for wall time (D18)."""

    def __init__(self, now: datetime = START) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


@pytest.fixture
def settings(fake_secrets: dict[str, str]):
    return build_settings(**fake_secrets)


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def limiter(settings, clock: Clock) -> SubmissionLimiter:
    return SubmissionLimiter(settings, clock=clock)


def test_the_configured_allowance_is_granted_up_front(limiter, settings) -> None:
    """A bucket starts full, so a user may submit their whole per-minute allowance at once."""
    allowed = [limiter.check(1).allowed for _ in range(settings.runtime.rate_limit_per_minute)]

    assert all(allowed)


def test_the_next_submission_after_the_allowance_is_refused(limiter, settings) -> None:
    for _ in range(settings.runtime.rate_limit_per_minute):
        limiter.check(1)

    decision = limiter.check(1)

    assert decision.allowed is False
    assert 0 < decision.retry_after <= 60.0


def test_a_refused_submission_does_not_cost_a_token(limiter, clock, settings) -> None:
    """Otherwise retrying immediately would eat the allowance the user is waiting for."""
    for _ in range(settings.runtime.rate_limit_per_minute):
        limiter.check(1)
    first = limiter.check(1).retry_after

    clock.advance(0.1)
    second = limiter.check(1).retry_after

    # The wait shrank by the time that passed, and by no more: a spent token would have moved it
    # by a whole token interval.
    assert second == pytest.approx(first - 0.1, abs=0.01)


def test_waiting_out_the_told_delay_allows_the_next_submission(limiter, clock) -> None:
    """The promise `retry_after` makes has to be true, or the message is a lie."""
    for _ in range(int(limiter.rate_per_minute)):
        limiter.check(1)
    refusal = limiter.check(1)

    clock.advance(refusal.retry_after)

    assert limiter.check(1).allowed is True


def test_the_allowance_refills_continuously(limiter, clock, settings) -> None:
    """Half a minute buys back half the allowance, which a fixed window could not express."""
    for _ in range(settings.runtime.rate_limit_per_minute):
        limiter.check(1)

    clock.advance(30.0)
    granted = sum(limiter.check(1).allowed for _ in range(50))

    assert granted == settings.runtime.rate_limit_per_minute // 2


def test_a_full_minute_restores_the_whole_allowance(limiter, clock, settings) -> None:
    for _ in range(settings.runtime.rate_limit_per_minute):
        limiter.check(1)
    assert limiter.tracked == 1

    clock.advance(60.0)

    assert limiter.prune() == 1, "a refilled bucket means the same as no bucket"
    assert limiter.tracked == 0
    assert limiter.check(1).allowed is True


def test_users_do_not_share_an_allowance(limiter, settings) -> None:
    for _ in range(settings.runtime.rate_limit_per_minute):
        limiter.check(1)

    assert limiter.check(2).allowed is True


def test_a_backwards_clock_does_not_mint_allowance(limiter, clock, settings) -> None:
    """An NTP correction must not hand out tokens that were never earned."""
    for _ in range(settings.runtime.rate_limit_per_minute):
        limiter.check(1)

    clock.advance(-3600.0)

    assert limiter.check(1).allowed is False


def test_forgetting_a_user_restores_their_allowance(limiter, settings) -> None:
    """For a state change that resets the situation, not for a retry after a refusal."""
    for _ in range(settings.runtime.rate_limit_per_minute):
        limiter.check(1)

    limiter.forget(1)

    assert limiter.check(1).allowed is True


def test_idle_users_are_not_retained(settings, clock) -> None:
    """Otherwise the dictionary would grow with every user the bot has ever seen."""
    limiter = SubmissionLimiter(settings, clock=clock)
    for user_id in range(500):
        limiter.check(user_id)
    assert limiter.tracked == 500

    clock.advance(120.0)

    assert limiter.prune() == 500
    assert limiter.tracked == 0


def test_the_dictionary_is_bounded_without_external_pruning(settings, clock) -> None:
    """`check` sweeps idle buckets itself once the dictionary grows, so the bound holds.

    Nobody has to remember to call `prune` for this to be true, which is what makes it
    trustworthy in a long-running process.
    """
    limiter = SubmissionLimiter(settings, clock=clock, prune_threshold=8)
    for user_id in range(20):
        limiter.check(user_id)

    assert limiter.tracked == 20, "nothing can be swept while every bucket is still in use"

    clock.advance(120.0)
    limiter.check(999)

    assert limiter.tracked == 1, "the sweep ran as part of check, leaving only the caller"


def test_a_negative_retry_after_is_never_reported(limiter, settings) -> None:
    for _ in range(settings.runtime.rate_limit_per_minute):
        limiter.check(1)

    assert limiter.check(1).retry_after > 0
