"""AC-6: `/status` lists the caller's live jobs, and only theirs.

The states it reports come from the queue's own rows, so these tests set up real jobs — queued,
claimed, cancelled — rather than passing in a fabricated status.
"""

from __future__ import annotations

import pytest

from app import config, db, queue, users
from handlers.job_status import status_command
from tests.fakes.messages import FakeClient, FakeMessage, FakeUser

LINK = "https://example.test/album/1"


@pytest.fixture
def using(settings, monkeypatch, clean):
    monkeypatch.setattr(config, "get_settings", lambda: settings)
    return settings


async def _status(user_id: int) -> FakeMessage:
    message = FakeMessage(FakeUser(user_id), command=("status",))
    await status_command(FakeClient(), message)
    return message


async def _register(*user_ids: int) -> None:
    async with db.session() as session:
        for user_id in user_ids:
            await users.upsert(session, user_id=user_id, first_name="T")


async def _queue_job(user_id: int, *, link: str = LINK) -> int:
    async with db.session() as session:
        job = await queue.enqueue(session, user_id=user_id, link=link, tier="free", priority=2.0)
        return job.id


async def test_nothing_running_is_reported_plainly(using) -> None:
    await _register(1)

    message = await _status(1)

    assert message.reply == "✅ No active downloads."


async def test_a_waiting_job_is_listed_as_waiting(using) -> None:
    await _register(1)
    job_id = await _queue_job(1)

    message = await _status(1)

    assert f"`{job_id}` — {LINK} — ⏳ Waiting for a slot" in message.reply


async def test_a_claimed_job_is_listed_as_running(using, settings) -> None:
    await _register(1)
    job_id = await _queue_job(1)
    async with db.session() as session:
        claimed = await queue.claim(session, settings)
        assert claimed is not None

    message = await _status(1)

    assert f"`{job_id}` — {LINK} — 🔄 Running" in message.reply


async def test_running_jobs_are_listed_before_waiting_ones(using, settings) -> None:
    await _register(1)
    first = await _queue_job(1)
    second = await _queue_job(1)
    async with db.session() as session:
        await queue.claim(session, settings)

    message = await _status(1)

    assert message.reply.index(f"`{first}`") < message.reply.index(f"`{second}`")


async def test_a_cancellation_in_progress_reads_as_cancelled(using, settings) -> None:
    """The user asked for it; showing "Running" while the worker unwinds would look broken."""
    await _register(1)
    job_id = await _queue_job(1)
    async with db.session() as session:
        await queue.claim(session, settings)
        await queue.request_cancel(session, job_id=job_id, user_id=1)

    message = await _status(1)

    assert f"`{job_id}` — {LINK} — ❌ Cancelled" in message.reply


async def test_finished_jobs_are_not_listed(using, settings) -> None:
    await _register(1)
    await _queue_job(1)
    async with db.session() as session:
        claimed = await queue.claim(session, settings)
        assert claimed is not None
        await queue.mark_delivered(session, claimed)

    message = await _status(1)

    assert message.reply == "✅ No active downloads."


async def test_another_users_jobs_are_not_listed(using) -> None:
    await _register(1, 2)
    await _queue_job(2)

    message = await _status(1)

    assert message.reply == "✅ No active downloads."


async def test_a_long_link_is_truncated_but_the_state_survives(using) -> None:
    await _register(1)
    long_link = "https://example.test/" + "x" * 300
    await _queue_job(1, link=long_link)

    message = await _status(1)

    assert "x" * 300 not in message.reply
    assert "…" in message.reply
    assert "⏳ Waiting for a slot" in message.reply


async def test_the_command_answers_an_unknown_caller(using) -> None:
    message = await _status(4242)

    assert message.reply == "✅ No active downloads."
