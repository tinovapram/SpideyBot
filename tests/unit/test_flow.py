"""The pipeline for one link (P3): the chain, the gate, the loop, and what the user is told.

The chain is driven with fake resolvers, because the chain's *logic* -- when to fall back, what to
report when everything fails, and the difference between an empty share and a broken link -- is
this module's own behaviour and must be observable without a network. The transfer loop is driven
against the loopback server from :mod:`tests.fakes.server`, because there the behaviour under test
really is the transport: which files land on disk, and what happens to the ones that fail.
"""

from __future__ import annotations

from pathlib import Path

from curl_cffi import requests

from app import catalog
from downloader import flow
from downloader.contracts import Resolution, ResolvedFile, ResolveError, Transfer
from downloader.fetcher import PROGRESS_STEP_BYTES
from downloader.gate import SizeCheck
from tests.fakes.server import FixtureServer, url

MB = 1024 * 1024

# ── fake resolvers ────────────────────────────────────────────────────────────


def item(name: str, *, size: int | None = 1000, url: str = "http://127.0.0.1:1/x") -> ResolvedFile:
    return ResolvedFile(
        transfers=(Transfer(url, "file", size),),
        filename=name,
        source_url="https://origin.test/page",
    )


def resolver(name: str, outcome: Resolution | Exception) -> flow.Resolver:
    """A chain entry that returns ``outcome``, or raises it."""

    async def attempt(_: str) -> Resolution:
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    return flow.Resolver(name=name, attempt=attempt)


def found(*items: ResolvedFile, engine: str = "gallerydl") -> Resolution:
    return Resolution(engine=engine, source_url="https://origin.test/page", files=items)


# ── items the fixture server can actually serve ───────────────────────────────


def served(server: FixtureServer, name: str, body: bytes) -> ResolvedFile:
    """An item whose transfer is served by the fixture server."""
    (server.root / name).write_bytes(body)
    return ResolvedFile(
        transfers=(Transfer(url(server, f"file/{name}"), "file", len(body)),),
        filename=name,
        source_url="https://origin.test/page",
    )


def broken(server: FixtureServer, name: str) -> ResolvedFile:
    """An item whose transfer 404s, so the file fails while the job continues."""
    return ResolvedFile(
        transfers=(Transfer(url(server, "missing"), "file", 4),),
        filename=name,
        source_url="https://origin.test/page",
    )


# ── the chain (E2) ────────────────────────────────────────────────────────────


async def test_the_first_resolver_that_finds_files_wins(staging: Path) -> None:
    calls: list[str] = []

    async def first(_: str) -> Resolution:
        calls.append("first")
        raise ResolveError("nope")

    async def second(_: str) -> Resolution:
        calls.append("second")
        return found(item("a.jpg"))

    async def never(_: str) -> Resolution:
        calls.append("never")
        return found(item("b.jpg"))

    result = await flow.download_link(
        "https://example.test/x",
        staging,
        limit_mb=500,
        chain=(
            flow.Resolver("native", first),
            flow.Resolver("gallery-dl", second),
            flow.Resolver("cyberdrop-dl", never),
        ),
    )

    assert calls == ["first", "second"]
    assert result.engine == "gallerydl"


async def test_the_fallback_notice_names_the_engine_being_tried(
    server: FixtureServer, staging: Path
) -> None:
    """E2's notice is the spec's sentence, with the name of whoever is actually running."""
    notices: list[str] = []

    result = await flow.download_link(
        "https://example.test/x",
        staging,
        limit_mb=500,
        chain=(
            resolver("native", ResolveError("the host refused")),
            resolver("gallery-dl", found(served(server, "a.jpg", b"jpeg"))),
        ),
        on_notice=notices.append,
    )

    assert notices == [catalog.fallback_notice("gallery-dl")]
    assert notices[0] == "⚠️ **SpideyBot:** Primary download failed, trying gallery-dl"
    assert result.delivered_anything is True


async def test_every_resolver_failing_reports_the_last_real_reason(staging: Path) -> None:
    """E2: report the reason, and prefer a failure's reason over "found nothing"."""
    result = await flow.download_link(
        "https://example.test/x",
        staging,
        limit_mb=500,
        chain=(
            resolver("native", ResolveError("the extractor reported 404 Not Found")),
            resolver("gallery-dl", ResolveError("nothing downloadable here", empty=True)),
        ),
    )

    assert result.reason == "the extractor reported 404 Not Found"
    assert result.empty_share is False
    assert result.stopped_early is True
    assert result.delivered_anything is False


async def test_nothing_found_anywhere_is_an_empty_share_not_an_error(staging: Path) -> None:
    """E4 has its own message (`catalog.EMPTY_SHARE`), so it must not be reported as a failure."""
    result = await flow.download_link(
        "https://example.test/x",
        staging,
        limit_mb=500,
        chain=(
            resolver("native", ResolveError("nothing here", empty=True)),
            resolver("gallery-dl", ResolveError("still nothing", empty=True)),
        ),
    )

    assert result.empty_share is True
    assert result.failures == ()
    # The reason is for the log, not the user: none of them named a failure, so there is nothing
    # more specific to report than "there was no media here".
    assert result.reason == "no downloadable media found at that link"


async def test_an_empty_resolution_moves_on_to_the_next_resolver(
    server: FixtureServer, staging: Path
) -> None:
    """A resolver that answers with no files has not helped, so the chain continues."""
    result = await flow.download_link(
        "https://example.test/x",
        staging,
        limit_mb=500,
        chain=(
            resolver("native", Resolution(engine="native", source_url="u", files=())),
            resolver("gallery-dl", found(served(server, "a.jpg", b"jpeg"))),
        ),
    )

    assert result.delivered_anything is True
    assert result.engine == "gallerydl"


async def test_an_empty_chain_is_a_reason_not_a_crash(staging: Path) -> None:
    result = await flow.download_link("https://example.test/x", staging, limit_mb=500, chain=())

    assert result.reason is not None
    assert result.delivered_anything is False


async def test_resolution_is_announced_before_downloading(staging: Path) -> None:
    """FR-7.3: the phases have to be visible, in order."""
    phases: list[str] = []

    await flow.download_link(
        "https://example.test/x",
        staging,
        limit_mb=500,
        chain=(resolver("gallery-dl", found(item("a.jpg", size=0))),),
        on_phase=phases.append,
    )

    assert phases == ["resolving", "downloading"]


# ── the size gate (AC-3, D31) ─────────────────────────────────────────────────


async def test_an_oversize_link_is_refused_before_any_transfer(staging: Path) -> None:
    """AC-3: the refusal carries both numbers and costs no bandwidth."""
    result = await flow.download_link(
        "https://example.test/x",
        staging,
        limit_mb=1,
        chain=(resolver("gallery-dl", found(item("a.bin", size=3 * MB))),),
    )

    assert isinstance(result.refused, SizeCheck)
    assert result.refused.allowed is False
    assert result.refused.actual_mb == "3"
    assert result.refused.limit_mb == "1"
    assert result.files == ()
    assert result.bytes_transferred == 0
    assert list(staging.iterdir()) == []


async def test_the_link_total_is_what_the_gate_judges(staging: Path) -> None:
    """AC-3 is about the whole share, so twenty small files can add up to a refusal."""
    many = tuple(item(f"{index}.bin", size=300 * 1024) for index in range(4))

    result = await flow.download_link(
        "https://example.test/x",
        staging,
        limit_mb=1,
        chain=(resolver("gallery-dl", found(*many)),),
    )

    assert result.refused is not None and result.refused.allowed is False
    assert result.files == ()


async def test_an_unlimited_tier_refuses_nothing(staging: Path) -> None:
    """0 means unlimited throughout the tier table, so a huge share passes (AC-3's second half)."""
    result = await flow.download_link(
        "https://example.test/x",
        staging,
        limit_mb=0,
        chain=(resolver("gallery-dl", found(item("a.bin", size=50 * 1024 * MB))),),
    )

    # It passed the gate, so the only reason nothing arrived is the transfer itself failing.
    assert result.refused is None
    assert result.reason is None


async def test_an_unmeasured_link_passes_but_is_capped_during_the_transfer(
    server: FixtureServer, staging: Path
) -> None:
    """D31: nobody reported a size, so the cap is enforced by the transfer instead."""
    size = 2 * MB
    unmeasured = ResolvedFile(
        transfers=(
            Transfer(
                url(server, f"unmeasured/{size}"),
            ),
        ),
        filename="big.bin",
        source_url="https://origin.test/page",
    )

    result = await flow.download_link(
        "https://example.test/x",
        staging,
        limit_mb=1,
        chain=(resolver("gallery-dl", found(unmeasured)),),
    )

    # Nothing was reported, so nothing could be refused; the transfer stopped at the cap instead.
    assert result.refused is None
    assert len(result.failures) == 1
    assert result.failures[0].reason == catalog.LIMIT_EXCEEDED_REASON.format(limit="1")
    assert result.files == ()
    assert result.bytes_transferred > MB
    assert list(staging.iterdir()) == []


# ── the download loop (FR-9, AC-15) ───────────────────────────────────────────


async def test_every_file_in_a_share_is_fetched(server: FixtureServer, staging: Path) -> None:
    items = tuple(served(server, f"{index}.bin", b"x" * (100 + index)) for index in range(3))

    result = await flow.download_link(
        "https://example.test/x",
        staging,
        limit_mb=500,
        chain=(resolver("gallery-dl", found(*items)),),
    )

    assert [f.path.name for f in result.files] == ["0.bin", "1.bin", "2.bin"]
    assert result.bytes_written == sum((100 + index) for index in range(3))
    assert result.bytes_transferred == result.bytes_written
    assert result.failures == ()
    assert sorted(p.name for p in staging.iterdir()) == ["0.bin", "1.bin", "2.bin"]


async def test_a_failing_file_does_not_stop_the_others(
    server: FixtureServer, staging: Path
) -> None:
    """FR-9.1/E5, and AC-15's shape: seventeen arrive while three are named."""
    items = (
        served(server, "good-1.bin", b"a" * 50),
        broken(server, "missing-1.bin"),
        served(server, "good-2.bin", b"b" * 70),
        broken(server, "missing-2.bin"),
    )

    result = await flow.download_link(
        "https://example.test/x",
        staging,
        limit_mb=500,
        chain=(resolver("gallery-dl", found(*items)),),
    )

    assert [f.path.name for f in result.files] == ["good-1.bin", "good-2.bin"]
    assert [f.name for f in result.failures] == ["missing-1.bin", "missing-2.bin"]
    assert all("404" in f.reason for f in result.failures)
    # The source URL travels with the failure so the final message can hyperlink it (FR-9.3).
    assert all(f.url == "https://origin.test/page" for f in result.failures)
    # A failure is not a reason to call the whole job a failure.
    assert result.reason is None
    assert result.delivered_anything is True


async def test_a_zero_byte_file_is_reported_with_the_spec_reason(
    server: FixtureServer, staging: Path
) -> None:
    """E6/FR-9.5: a 0-byte result is a failure with the exact wording the spec fixes."""
    empty = ResolvedFile(
        transfers=(Transfer(url(server, "empty"), "file", 0),),
        filename="nothing.bin",
    )

    result = await flow.download_link(
        "https://example.test/x",
        staging,
        limit_mb=500,
        chain=(resolver("gallery-dl", found(empty)),),
    )

    assert result.files == ()
    assert [f.reason for f in result.failures] == [catalog.EMPTY_FILE_REASON]


async def test_two_files_with_the_same_name_both_survive(
    server: FixtureServer, staging: Path
) -> None:
    """Otherwise the second overwrites the first and the user receives one file of two.

    Two *different* transfers that happen to carry the same title — which a share does as easily as
    it does one file, since the name comes from the page's metadata rather than from the URL.
    """
    (server.root / "one").mkdir()
    (server.root / "two").mkdir()
    first = ResolvedFile(
        transfers=(Transfer(url(server, "file/one/same.bin"), "file", 5),), filename="same.bin"
    )
    second = ResolvedFile(
        transfers=(Transfer(url(server, "file/two/same.bin"), "file", 6),), filename="same.bin"
    )
    (server.root / "one" / "same.bin").write_bytes(b"first")
    (server.root / "two" / "same.bin").write_bytes(b"second")

    result = await flow.download_link(
        "https://example.test/x",
        staging,
        limit_mb=500,
        chain=(resolver("gallery-dl", found(first, second)),),
    )

    assert [f.path.name for f in result.files] == ["same.bin", "same (2).bin"]
    assert (staging / "same.bin").read_bytes() == b"first"
    assert (staging / "same (2).bin").read_bytes() == b"second"


async def test_a_failed_file_frees_its_name_for_a_later_one(
    server: FixtureServer, staging: Path
) -> None:
    """A number is only spent when the file actually lands, so a later one keeps the plain name."""
    items = (broken(server, "same.bin"), served(server, "same.bin", b"second"))

    result = await flow.download_link(
        "https://example.test/x",
        staging,
        limit_mb=500,
        chain=(resolver("gallery-dl", found(*items)),),
    )

    assert [f.path.name for f in result.files] == ["same.bin"]


async def test_a_hostile_filename_cannot_escape_the_staging_directory(
    server: FixtureServer, staging: Path
) -> None:
    """Titles come from the internet, and a path separator in one would write outside the job."""
    # Served from a subdirectory so that `staging.parent` holds nothing but the staging directory:
    # otherwise this test would find the fixture's own source file and call it an escape.
    (server.root / "src").mkdir()
    (server.root / "src" / "escape.bin").write_bytes(b"x")
    hostile = ResolvedFile(
        transfers=(Transfer(url(server, "file/src/escape.bin"), "file", 1),),
        filename="../../escape.bin",
    )

    result = await flow.download_link(
        "https://example.test/x",
        staging,
        limit_mb=500,
        chain=(resolver("gallery-dl", found(hostile)),),
    )

    # Separators become underscores, and the leading dots go too — `safe_filename` strips them so a
    # hostile title cannot produce a hidden file either.
    assert [f.path.name for f in result.files] == ["_.._escape.bin"]
    assert result.files[0].path.parent == staging
    # And nothing was written next to the staging directory.
    assert not (staging.parent / "escape.bin").exists()


async def test_progress_carries_the_file_it_belongs_to(
    server: FixtureServer, staging: Path
) -> None:
    """FR-7.5's ``file 2/3``: the fetcher's numbers, plus where this file sits in the job."""
    body = b"p" * (2 * PROGRESS_STEP_BYTES + 1024)
    items = (served(server, "one.bin", b"q"), served(server, "two.bin", body))
    seen: list[flow.JobProgress] = []

    await flow.download_link(
        "https://example.test/x",
        staging,
        limit_mb=500,
        chain=(resolver("gallery-dl", found(*items)),),
        on_progress=seen.append,
    )

    assert seen[0].file_counter == "1/2"
    assert seen[-1].file_counter == "2/2"
    assert seen[-1].filename == "two.bin"
    assert seen[-1].transfer.transfer_done == len(body)
    # The wrapper must not change the fetcher's own numbers.
    assert seen[-1].transfer.overall_done == len(body)


# ── cancellation (FR-11.6) ────────────────────────────────────────────────────


async def test_cancellation_between_files_keeps_nothing_pending(staging: Path) -> None:
    stopped = False

    async def attempt(_: str) -> Resolution:
        nonlocal stopped
        stopped = True
        return found(item("a.jpg", size=0), item("b.jpg", size=0))

    result = await flow.download_link(
        "https://example.test/x",
        staging,
        limit_mb=500,
        chain=(flow.Resolver("gallery-dl", attempt),),
        should_stop=lambda: stopped,
    )

    assert result.cancelled is True
    assert result.files == ()
    assert result.failures == ()


async def test_cancelling_mid_transfer_is_not_a_file_failure(
    server: FixtureServer, staging: Path
) -> None:
    """FR-11.6 stops the transfer, and a cancelled file is not something that went wrong."""
    size = PROGRESS_STEP_BYTES * 4
    slow = ResolvedFile(
        transfers=(Transfer(url(server, f"slow/{size}"), "file", size),),
        filename="slow.bin",
        source_url="https://origin.test/page",
    )
    reported = 0

    def should_stop() -> bool:
        return reported > 0

    async def progress(_: flow.JobProgress) -> None:
        nonlocal reported
        reported += 1

    result = await flow.download_link(
        "https://example.test/x",
        staging,
        limit_mb=500,
        chain=(resolver("gallery-dl", found(slow)),),
        on_progress=progress,
        should_stop=should_stop,
    )

    assert result.cancelled is True
    assert result.failures == ()
    assert result.files == ()
    # The partial is discarded, not left behind to be uploaded as a truncated file.
    assert list(staging.iterdir()) == []


async def test_the_session_outlives_every_file_of_the_job(
    server: FixtureServer, staging: Path
) -> None:
    """One pool per job, not one per file: the fetcher must not close a caller's session."""
    items = tuple(served(server, f"{index}.bin", b"z") for index in range(3))

    async with requests.AsyncSession() as client:
        result = await flow.download_link(
            "https://example.test/x",
            staging,
            limit_mb=500,
            chain=(resolver("gallery-dl", found(*items)),),
            session=client,
        )
        response = await client.get(url(server, "empty"), impersonate="chrome", timeout=10)

    assert len(result.files) == 3
    assert response.status_code == 200


async def test_the_staging_directory_is_created_if_missing(
    server: FixtureServer, staging: Path
) -> None:
    nested = staging / "job" / "42"

    result = await flow.download_link(
        "https://example.test/x",
        nested,
        limit_mb=500,
        chain=(resolver("gallery-dl", found(served(server, "a.bin", b"a"))),),
    )

    assert result.files[0].path.parent == nested
