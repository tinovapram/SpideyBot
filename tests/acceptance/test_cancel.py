"""AC-5: `/cancel` refuses what it must, and actually stops what it can.

Driven through the real handler against a real database, because the interesting part is the
*database's* answer to the ownership and state questions — the handler is only allowed to relay
it. A test with a stubbed queue would verify the copy and nothing else.
"""

from __future__ import annotations

import pytest

from app import config, db, queue, users
from app.models import Job
from handlers.cancel import cancel_command
from tests.fakes.messages import FakeClient, FakeMessage, FakeUser

LINK = "https://example.test/album/1"


@pytest.fixture
def using(settings, monkeypatch, clean):
    monkeypatch.setattr(config, "get_settings", lambda: settings)
    return settings


async def _cancel(user_id: int, *arguments: str) -> FakeMessage:
    message = FakeMessage(FakeUser(user_id), command=("cancel", *arguments))
    await cancel_command(FakeClient(), message)
    return message


async def _register(*user_ids: int) -> None:
    async with db.session() as session:
        for user_id in user_ids:
            await users.upsert(session, user_id=user_id, first_name="T")


async def _queue_job(user_id: int) -> int:
    async with db.session() as session:
        job = await queue.enqueue(session, user_id=user_id, link=LINK, tier="free", priority=2.0)
        return job.id


async def _claim(job_id: int, settings) -> None:
    async with db.session() as session:
        claimed = await queue.claim(session, settings)
        assert claimed is not None and claimed.id == job_id


async def _job(job_id: int) -> Job:
    async with db.session() as session:
        return await session.get(Job, job_id)


# ── refusals (FR-11.1 - FR-11.4) ────────────────────────────────────────


async def test_no_id_shows_usage(using) -> None:
    await _register(1)

    message = await _cancel(1)

    assert message.reply == "**Usage:** /cancel *ID*\n\nView your active downloads with /status."


@pytest.mark.parametrize("raw", ["abc", "-5", "4.2", ""])
async def test_a_non_numeric_id_points_at_status(using, raw: str) -> None:
    await _register(1)

    message = await _cancel(1, raw)

    assert message.reply == "⚠️ **Invalid ID** — pass the number shown by /status."


async def test_an_unknown_id_is_refused(using) -> None:
    await _register(1)

    message = await _cancel(1, "999")

    assert message.reply == "⚠️ No active download with ID `999`."


async def test_another_users_job_cannot_be_cancelled(using, settings) -> None:
    """Refused *and* left alone: a refusal that cancelled the job anyway would be worse."""
    await _register(1, 2)
    job_id = await _queue_job(2)
    await _claim(job_id, settings)

    message = await _cancel(1, str(job_id))

    assert message.reply == "⚠️ That download belongs to another user."
    job = await _job(job_id)
    assert job.status == queue.RUNNING
    assert job.cancel_requested_at is None


async def test_a_delivered_job_has_nothing_left_to_cancel(using, settings) -> None:
    await _register(1)
    job_id = await _queue_job(1)
    async with db.session() as session:
        claimed = await queue.claim(session, settings)
        assert claimed is not None
        await queue.mark_delivered(session, claimed)

    message = await _cancel(1, str(job_id))

    assert message.reply == f"⚠️ No active download with ID `{job_id}`."
    assert (await _job(job_id)).status == queue.DELIVERED


# ── it actually stops the job (FR-11.5, FR-11.6) ────────────────────────


async def test_a_waiting_job_is_cancelled_immediately(using) -> None:
    """No worker will ever pick it up, so nothing has to wait for one."""
    await _register(1)
    job_id = await _queue_job(1)

    message = await _cancel(1, str(job_id))

    assert message.reply == f"❌ Cancelled download **{job_id}**."
    assert (await _job(job_id)).status == queue.CANCELLED


async def test_a_running_job_is_asked_to_stop(using, settings) -> None:
    """A database write cannot interrupt a transfer, so the worker is told to stop (FR-11.6)."""
    await _register(1)
    job_id = await _queue_job(1)
    await _claim(job_id, settings)

    message = await _cancel(1, str(job_id))

    assert message.reply == f"❌ Cancelled download **{job_id}**."
    job = await _job(job_id)
    assert job.status == queue.RUNNING, "the worker still has to unwind the job"
    assert job.cancel_requested_at is not None, "the stop request is what the worker polls"


async def test_a_cancelled_job_leaves_the_status_list(using) -> None:
    """The two commands have to agree: /cancel then /status must not still show the job."""
    await _register(1)
    job_id = await _queue_job(1)

    await _cancel(1, str(job_id))

    async with db.session() as session:
        assert await queue.active_jobs(session, 1) == []
