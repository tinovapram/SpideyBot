"""The pipeline as the user experiences it (AC-2, AC-3, AC-15, FR-8, FR-9).

These tests drive the real handler, the real pipeline, the real ledger (against a real Postgres)
and the real delivery rules against a recording client. Only two things are faked, and both would
otherwise leave the machine: the *chain* that resolves a link, and Telegram itself. The bytes are
real -- they come from the loopback fixture server the fetcher's tests use -- so "three of five
files arrived" is a statement about files on disk rather than about a mock's return value.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from sqlalchemy import select

from app import catalog, jobs, paths, queue, users, workers
from app import db as db_module
from app.config import Settings
from app.models import Job, JobItem
from downloader import flow
from downloader.contracts import Resolution, ResolvedFile, ResolveError, Transfer, kind_for
from downloader.fetcher import FetchedFile
from tests.fakes.messages import RecordingClient
from tests.fakes.server import FixtureServer, url

MB = 1024 * 1024
LINK = "https://origin.test/page"


async def register(user_id: int) -> None:
    """The job table has a foreign key to users, so a job needs a real user."""
    async with db_module.session() as session:
        await users.upsert(session, user_id=user_id, first_name="T")


def served(server: FixtureServer, name: str, body: bytes) -> ResolvedFile:
    """A file the fixture server can actually deliver.

    Classified by extension exactly as ``probe`` does, because FR-8.1's "preserve the type" happens
    here: a helper that left every kind as ``document`` would make delivery look broken when the
    resolve step was simply unrealistic.
    """
    (server.root / name).write_bytes(body)
    return ResolvedFile(
        transfers=(Transfer(url(server, f"file/{name}"), "file", len(body)),),
        filename=name,
        media_kind=kind_for(Path(name).suffix),
        source_url=LINK,
    )


def broken(server: FixtureServer, name: str) -> ResolvedFile:
    """A file whose transfer 404s, so it fails while the rest of the job continues."""
    return ResolvedFile(
        transfers=(Transfer(url(server, "missing"), "file", 4),),
        filename=name,
        source_url=LINK,
    )


def chain_of(*files: ResolvedFile) -> tuple[flow.Resolver, ...]:
    """A chain that resolves to exactly these files, so nothing here depends on a live host."""

    async def attempt(_: str) -> Resolution:
        return Resolution(engine="gallerydl", source_url=LINK, files=files)

    return (flow.Resolver(name="gallery-dl", attempt=attempt),)


class FakeKeeper:
    """The parts of ``LeaseKeeper`` a handler uses."""

    def __init__(self) -> None:
        self.stop_requested = False


async def run_job(
    settings: Settings,
    *,
    user_id: int,
    chain: tuple[flow.Resolver, ...],
    client: RecordingClient,
    delegate: flow.DelegatingFetch | None = None,
    tier: str = "free",
) -> Job:
    """Submit, claim as the pool would, run the handler, and hand back the finished job."""
    await register(user_id)
    async with db_module.session() as session:
        job = await queue.enqueue(session, user_id=user_id, link=LINK, tier=tier)
    async with db_module.session() as session:
        claimed = await queue.claim(session, settings)
        assert claimed is not None and claimed.id == job.id
        claimed.status_chat_id = user_id
        claimed.status_message_id = 4242

    handler = jobs.build_handler(
        settings,
        sender_for=lambda _: client,
        chain_for=lambda *_: chain,
        delegate_for=lambda *_: delegate,
    )

    await workers.execute(job.id, settings, handler)

    async with db_module.session() as fresh:
        finished = await fresh.get(Job, job.id)
        assert finished is not None
    return finished


# ── AC-2: a link that works ───────────────────────────────────────────────────


async def test_ac2_a_successful_link_delivers_its_files_and_reports_the_count(
    clean, settings, server
) -> None:
    files = (
        served(server, "one.jpg", b"a" * 100),
        served(server, "two.mp4", b"b" * 200),
        served(server, "three.pdf", b"c" * 300),
    )
    client = RecordingClient()

    job = await run_job(settings, user_id=1, chain=chain_of(*files), client=client)

    assert job.status == queue.DELIVERED
    assert sorted(client.files) == ["one.jpg", "three.pdf", "two.mp4"]
    assert client.texts[-1].startswith("✅")
    assert "3/3" in client.texts[-1]
    # FR-8.4: everything delivered points at the command the user sent.
    assert all(message.kwargs.get("reply_to_message_id") == 4242 for message in client.sent)
    # FR-8.3: each file carries its source link.
    captions = [message.caption for message in client.sent if message.caption]
    assert len(captions) == 3
    assert all("origin.test" in caption for caption in captions)


async def test_ac2_the_kind_survives_delivery(clean, settings, server) -> None:
    """FR-8.1: a lone photo is sent as a photo, a video as a video, the rest as documents."""
    methods = {}
    for name in ("a.jpg", "b.mp4", "c.zip"):
        client = RecordingClient()
        await run_job(
            settings, user_id=1, chain=chain_of(served(server, name, b"x")), client=client
        )
        methods[name] = client.sent[0].method

    assert methods == {
        "a.jpg": "send_photo",
        "b.mp4": "send_video",
        "c.zip": "send_document",
    }


async def test_ac2_an_album_keeps_each_items_kind(clean, settings, server) -> None:
    """In an album the kind lives in the ``InputMedia`` type, not in the method called."""
    client = RecordingClient()

    await run_job(
        settings,
        user_id=1,
        chain=chain_of(served(server, "a.jpg", b"x"), served(server, "b.mp4", b"y")),
        client=client,
    )

    kinds = {
        Path(str(item.media)).name: type(item).__name__
        for message in client.sent
        for item in message.media
    }
    assert kinds == {"a.jpg": "InputMediaPhoto", "b.mp4": "InputMediaVideo"}


async def test_ac2_an_album_holds_up_to_ten_files(clean, settings, server) -> None:
    """FR-8.2: albums of ten, and the remainder is flushed rather than dropped."""
    files = tuple(served(server, f"{index:02d}.jpg", b"x" * 10) for index in range(12))
    client = RecordingClient()

    await run_job(settings, user_id=1, chain=chain_of(*files), client=client)

    albums = client.groups
    assert sorted(len(members) for members in albums.values()) == [2, 10], (
        "twelve files are an album of ten and a flushed remainder of two"
    )
    assert len(client.files) == 12


async def test_ac2_the_ledger_records_what_was_delivered(clean, settings, server) -> None:
    """8.2.3: the ledger is what makes a resumed attempt safe, so it must be written."""
    job = await run_job(
        settings,
        user_id=1,
        chain=chain_of(served(server, "a.jpg", b"x")),
        client=RecordingClient(),
    )

    async with db_module.session() as fresh:
        rows = (
            (await fresh.execute(select(JobItem).where(JobItem.job_id == job.id))).scalars().all()
        )

    assert [(row.filename, row.state) for row in rows] == [("a.jpg", jobs.DELIVERED)]
    assert rows[0].bytes_total == 1


# ── AC-3: the size limit ──────────────────────────────────────────────────────


async def test_ac3_an_oversize_link_sends_nothing_and_names_both_numbers(
    clean, settings, server
) -> None:
    """AC-3: refused before transfer, with the actual size and the allowed one."""
    client = RecordingClient()
    huge = ResolvedFile(
        transfers=(
            Transfer(
                url(server, "unmeasured/1"),
            ),
        ),
        filename="huge.bin",
        filesize=600 * MB,
    )

    job = await run_job(settings, user_id=1, chain=chain_of(huge), client=client)

    assert client.files == [], "AC-3 refuses before a single byte moves"
    assert server.requests == [], "no transfer was even attempted"
    assert job.status == queue.FAILED
    body = client.texts[-1]
    assert body == catalog.limit_exceeded_message(actual_mb="600", limit_mb="500")


async def test_ac3_a_refusal_does_not_cost_the_user_a_download(clean, settings, server) -> None:
    """FR-3.2: nothing was delivered, so the day's reservation is handed back."""
    from app import quota

    huge = ResolvedFile(
        transfers=(
            Transfer(
                url(server, "unmeasured/1"),
            ),
        ),
        filename="huge.bin",
        filesize=600 * MB,
    )

    await run_job(settings, user_id=1, chain=chain_of(huge), client=RecordingClient())

    async with db_module.session() as fresh:
        usage = await quota.current(fresh, 1)

    assert usage.bytes_in == 0
    assert usage.downloads == 0


async def test_ac3_the_same_link_passes_on_an_unlimited_tier(clean, settings, server) -> None:
    """AC-3's second half: unlimited means unlimited, and the file really arrives."""
    client = RecordingClient()
    body = b"z" * 4096
    (server.root / "big.bin").write_bytes(body)
    big = ResolvedFile(
        transfers=(Transfer(url(server, "file/big.bin"), "file", len(body)),),
        filename="big.bin",
        filesize=600 * MB,
    )

    job = await run_job(settings, user_id=1, chain=chain_of(big), client=client, tier="premium")

    assert job.status == queue.DELIVERED
    assert client.files == ["big.bin"]


# ── AC-15: failure transparency ───────────────────────────────────────────────


async def test_ac15_named_failures_arrive_alongside_the_files_that_worked(
    clean, settings, server
) -> None:
    """AC-15's shape: the ones that failed are named, and the rest still arrive."""
    files = (
        served(server, "good-1.jpg", b"a"),
        broken(server, "gone-1.jpg"),
        served(server, "good-2.jpg", b"b"),
        broken(server, "gone-2.jpg"),
        served(server, "good-3.jpg", b"c"),
    )
    client = RecordingClient()

    job = await run_job(settings, user_id=1, chain=chain_of(*files), client=client)

    assert job.status == queue.DELIVERED, "three files reached the user, so the job succeeded"
    assert sorted(client.files) == ["good-1.jpg", "good-2.jpg", "good-3.jpg"]
    body = client.texts[-1]
    # FR-7.9's result line, then FR-9.4's block naming each failure.
    assert "Sent 3/5 files" in body
    assert "2 file(s) failed:" in body
    assert "gone-1.jpg" in body and "gone-2.jpg" in body
    assert "404" in body
    # FR-9.3: a failure with a known URL is hyperlinked.
    assert "[gone-1.jpg](https://origin.test/page)" in body


async def test_ac15_a_job_whose_every_file_failed_is_a_failure(clean, settings, server) -> None:
    """Spec 8.1: Failed means *no files delivered*, not "no files attempted"."""
    client = RecordingClient()

    job = await run_job(
        settings,
        user_id=1,
        chain=chain_of(broken(server, "gone-1.jpg"), broken(server, "gone-2.jpg")),
        client=client,
    )

    # Every file failed, so nothing was delivered: the job is not a success (spec 8.1). It is
    # *queued*, not failed, because a host that 404s once may answer on the next attempt.
    assert job.status == queue.QUEUED
    assert client.files == []
    assert "⚠️ 2 file(s) failed:" in client.texts[-1]


async def test_a_file_telegram_refuses_does_not_cost_the_others(clean, settings, server) -> None:
    """FR-9.1: a delivery failure is one file's problem, not the job's."""
    client = RecordingClient(fail={"two.jpg"})

    job = await run_job(
        settings,
        user_id=1,
        chain=chain_of(
            served(server, "one.jpg", b"a"),
            served(server, "two.jpg", b"b"),
            served(server, "three.jpg", b"c"),
        ),
        client=client,
    )

    assert sorted(client.files) == ["one.jpg", "three.jpg"]
    assert job.status == queue.DELIVERED


async def test_bytes_are_charged_for_what_the_network_moved(clean, settings, server) -> None:
    """FR-3.2/D35: the bandwidth the job really consumed, which is what the cap measures."""
    from app import quota

    body = b"x" * 5000

    await run_job(
        settings,
        user_id=1,
        chain=chain_of(served(server, "a.bin", body)),
        client=RecordingClient(),
    )

    async with db_module.session() as fresh:
        usage = await quota.current(fresh, 1)

    assert usage.bytes_in == len(body)


# ── the ledger across attempts (FR-8.5, 8.2) ──────────────────────────────────


async def test_a_second_attempt_does_not_deliver_the_same_file_again(
    clean, settings, server
) -> None:
    """FR-8.5/E26: what the first attempt delivered is never delivered a second time."""
    item = served(server, "a.jpg", b"x")
    client = RecordingClient()

    job = await run_job(settings, user_id=1, chain=chain_of(item), client=client)
    assert client.files == ["a.jpg"]

    # A second run of the same job: the shape a lost lease or a restart produces.
    async with db_module.session() as fresh:
        again = await fresh.get(Job, job.id)
        assert again is not None
        again.status = queue.RUNNING

    handler = jobs.build_handler(
        settings,
        sender_for=lambda _: client,
        chain_for=lambda *_: chain_of(item),
        delegate_for=lambda *_: None,
    )
    await workers.execute(job.id, settings, handler)

    assert client.files == ["a.jpg"], "the file was delivered once, not twice"

    async with db_module.session() as fresh:
        rows = (
            (await fresh.execute(select(JobItem).where(JobItem.job_id == job.id))).scalars().all()
        )

    assert len([row for row in rows if row.state == jobs.DELIVERED]) == 1


async def test_the_last_resort_delivers_when_the_chain_finds_nothing(
    clean, settings, tmp_path
) -> None:
    """D38 end to end: a delegated file reaches the user through the same delivery path."""
    client = RecordingClient()
    delegated = tmp_path / "from-cyberdrop.bin"
    delegated.write_bytes(b"delegated")

    async def run(url: str, staging: Path, **_: Any) -> tuple[FetchedFile, ...]:
        return (
            FetchedFile(
                path=delegated, media_kind="document", bytes_written=9, note="cyberdrop-dl"
            ),
        )

    async def failing(_: str) -> Resolution:
        raise ResolveError("every measured method failed")

    job = await run_job(
        settings,
        user_id=1,
        chain=(flow.Resolver(name="gallery-dl", attempt=failing),),
        client=client,
        delegate=flow.DelegatingFetch(name="cyberdrop-dl", run=run),
    )

    assert job.status == queue.DELIVERED
    assert client.files == ["from-cyberdrop.bin"]


def test_the_job_staging_directory_is_the_one_the_paths_module_owns() -> None:
    """The handler and the sweeper must agree, or a job's files would outlive their job (D21)."""
    assert paths.job_tmp_dir(7).name == "7"
    assert paths.job_tmp_dir(7).parent == paths.TMP_DIR
