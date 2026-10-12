"""Fetching the bytes ourselves (D25, D31, FR-7): streaming, merging, and the failures that matter.

Every test here talks to a real HTTP server on loopback and writes real files, because the
behaviour under test *is* the transport: whether headers arrive, whether a cap stops a transfer
mid-flight, whether what ffmpeg produced is playable. The server lives in :mod:`tests.fakes.server`
so the pipeline's tests drive the same one rather than a second copy of it.
"""

from __future__ import annotations

import ast
import pathlib
import shutil
import subprocess
from pathlib import Path

import pytest
from curl_cffi import requests

from app import catalog
from downloader import fetcher
from downloader.contracts import ResolvedFile, Transfer
from downloader.fetcher import (
    EmptyFile,
    FetchedFile,
    FetchError,
    FetchProgress,
    SizeLimitExceeded,
    TransferCancelled,
)
from tests.fakes.server import FixtureServer, url

MB = 1024 * 1024


def file_item(server: FixtureServer, name: str, **overrides: object) -> ResolvedFile:
    """A single-transfer item for one file the fixture server is serving."""
    fields: dict[str, object] = {
        "transfers": (Transfer(url(server, f"file/{name}")),),
        "filename": name,
    }
    fields.update(overrides)
    return ResolvedFile(**fields)  # type: ignore[arg-type]


# ── media produced by ffmpeg, so the merge is verified by a real demuxer ──


def _ffmpeg(*args: str) -> None:
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *args],
        check=True,
        capture_output=True,
    )


@pytest.fixture(scope="session")
def media(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """One second of real video, real audio, and something that is neither."""
    if shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None:
        pytest.skip("ffmpeg and ffprobe are required to verify a merge")

    directory = tmp_path_factory.mktemp("media")
    _ffmpeg(
        "-f",
        "lavfi",
        "-i",
        "testsrc=duration=1:size=128x72:rate=10",
        "-c:v",
        "libx264",
        "-pix_fmt",
        "yuv420p",
        str(directory / "video.mp4"),
    )
    _ffmpeg(
        "-f",
        "lavfi",
        "-i",
        "sine=frequency=440:duration=1",
        "-c:a",
        "aac",
        str(directory / "audio.m4a"),
    )
    (directory / "corrupt.m4a").write_bytes(b"not audio at all\n" * 64)
    return directory


@pytest.fixture
def media_server(server: FixtureServer, media: Path) -> FixtureServer:
    """The fixture server, rooted at the generated media."""
    server.root = media
    return server


def streams_in(path: Path) -> list[str]:
    """The codec types a real demuxer finds, which is what "playable" means here."""
    completed = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "stream=codec_type",
            "-of",
            "csv=p=0",
            str(path),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return sorted(line.strip() for line in completed.stdout.splitlines() if line.strip())


def merge_item(
    server: FixtureServer,
    *,
    video: str = "video.mp4",
    audio: str = "audio.m4a",
    filename: str = "merged.mp4",
) -> ResolvedFile:
    """A two-transfer item: exactly what every YouTube job now looks like (D30)."""
    video_bytes = (server.root / video).stat().st_size
    audio_bytes = (server.root / audio).stat().st_size
    return ResolvedFile(
        transfers=(
            Transfer(url(server, f"file/{video}"), "video", video_bytes),
            Transfer(url(server, f"file/{audio}"), "audio", audio_bytes),
        ),
        filename=filename,
        media_kind="video",
    )


def generated_item(
    server: FixtureServer, size: int, *, filename: str = "generated.bin"
) -> ResolvedFile:
    """One transfer of ``size`` bytes with no size reported by any extractor."""
    return ResolvedFile(
        transfers=(Transfer(url(server, f"padded/{size}")),),
        filename=filename,
    )


# ── one transfer ─────────────────────────────────────────────────────────


async def test_a_single_transfer_is_streamed_to_the_destination(
    server: FixtureServer, tmp_path: Path
) -> None:
    body = b"spidey" * 1000
    (server.root / "payload.bin").write_bytes(body)
    item = file_item(server, "payload.bin", media_kind="video", source_url="https://example.test/1")
    destination = tmp_path / "out" / "payload.bin"

    result = await fetcher.fetch(item, destination)

    assert isinstance(result, FetchedFile)
    assert destination.read_bytes() == body
    assert result.path == destination
    assert result.bytes_written == len(body)
    assert result.media_kind == "video"
    assert result.merged is False
    assert result.source_url == "https://example.test/1"


async def test_the_destination_directory_is_created(server: FixtureServer, tmp_path: Path) -> None:
    """The staging directory may not exist yet, and fetching should not assume it does."""
    (server.root / "payload.bin").write_bytes(b"abc")

    result = await fetcher.fetch(file_item(server, "payload.bin"), tmp_path / "a" / "b" / "out.bin")

    assert result.path.read_bytes() == b"abc"


async def test_browser_headers_are_sent_and_impersonation_supplies_a_user_agent(
    server: FixtureServer, tmp_path: Path
) -> None:
    """Measured against the server's own record, not against what we intended to send."""
    (server.root / "payload.bin").write_bytes(b"abc")

    await fetcher.fetch(file_item(server, "payload.bin"), tmp_path / "out.bin")

    sent = {name.lower(): value for name, value in server.requests[0].items()}
    assert sent["accept"] == "*/*"
    assert "en-US" in sent["accept-language"]
    # Impersonation is what makes this a real browser's UA rather than ours (anti-ban).
    assert "Chrome" in sent["user-agent"]
    # Loopback is not a host with a rule, so nothing is invented for it.
    assert "referer" not in sent


def test_host_headers_are_added_only_for_hosts_that_need_them() -> None:
    """The media host is not the page host, which is why both are listed (D25's headers in code)."""
    youtube = "https://www.youtube.com/"

    assert fetcher.headers_for("https://www.youtube.com/watch?v=x")["Referer"] == youtube
    assert fetcher.headers_for(youtube + "x")["Referer"] == youtube
    assert (
        fetcher.headers_for("https://rr3---sn-abc.googlevideo.com/videoplayback?x=1")["Referer"]
        == youtube
    )
    assert fetcher.headers_for("https://x.com/u/status/1")["Referer"] == "https://x.com/"

    assert "Referer" not in fetcher.headers_for("https://example.test/a.mp4")
    # A suffix match must not swallow a lookalike host.
    assert "Referer" not in fetcher.headers_for("https://notyoutube.com/a.mp4")


async def test_an_injected_session_is_reused_rather_than_closed(
    server: FixtureServer, tmp_path: Path
) -> None:
    """A caller-owned session outlives the fetch: one pool per worker, not per file."""
    (server.root / "payload.bin").write_bytes(b"abc")

    async with requests.AsyncSession() as client:
        await fetcher.fetch(file_item(server, "payload.bin"), tmp_path / "out.bin", session=client)
        response = await client.get(url(server, "empty"), impersonate="chrome", timeout=10)

    assert response.status_code == 200


# ── progress (FR-7.4) ────────────────────────────────────────────────────


async def test_progress_is_reported_in_steps_and_ends_at_the_total(
    server: FixtureServer, tmp_path: Path
) -> None:
    size = 2 * fetcher.PROGRESS_STEP_BYTES + 4096
    seen: list[FetchProgress] = []

    async def hook(update: FetchProgress) -> None:
        seen.append(update)

    result = await fetcher.fetch(generated_item(server, size), tmp_path / "gen.bin", progress=hook)

    assert result.bytes_written == size
    # Coalesced, not one call per chunk: the status layer redraws on its own interval anyway.
    assert 2 <= len(seen) <= 6
    assert seen[0].transfer_done >= fetcher.PROGRESS_STEP_BYTES
    assert [u.transfer_done for u in seen] == sorted(u.transfer_done for u in seen)
    assert len({u.transfer_done for u in seen}) == len(seen)
    assert seen[-1].transfer_done == size
    assert seen[-1].transfer_total == size
    assert seen[-1].overall_done == size
    # No extractor size, but a single transfer *is* the file, so the total is the server's.
    assert seen[-1].overall_total == size
    assert seen[-1].url == url(server, f"padded/{size}")
    assert seen[-1].role == "file"


async def test_bytes_are_written_as_they_arrive_rather_than_buffered_whole(
    server: FixtureServer, tmp_path: Path
) -> None:
    """The memory guarantee, measured: progress arrives while the server is still sending.

    curl_cffi ignores ``chunk_size`` (it warns), so the chunking is libcurl's — but if the body
    were gathered before being written, no callback could fire until the last byte landed.
    """
    size = fetcher.PROGRESS_STEP_BYTES * 3
    item = ResolvedFile(transfers=(Transfer(url(server, f"slow/{size}")),), filename="slow.bin")
    destination = tmp_path / "slow.bin"
    written_at_first_report: list[int] = []

    async def hook(update: FetchProgress) -> None:
        written_at_first_report.append(destination.stat().st_size)

    await fetcher.fetch(item, destination, progress=hook)

    assert len(written_at_first_report) >= 2
    assert 0 < written_at_first_report[0] < size
    assert destination.stat().st_size == size


async def test_a_plain_function_works_as_a_progress_hook(
    server: FixtureServer, tmp_path: Path
) -> None:
    """Not every caller wants a coroutine, and a hook that returns None must not be awaited."""
    seen: list[FetchProgress] = []

    await fetcher.fetch(generated_item(server, 4096), tmp_path / "gen.bin", progress=seen.append)

    assert seen[-1].transfer_done == 4096


async def test_a_size_the_extractor_never_reported_is_taken_from_the_server(
    server: FixtureServer, tmp_path: Path
) -> None:
    """D31's premise: the transfer runs, but the denominator is still knowable."""
    size = 300_000
    item = generated_item(server, size)
    assert item.expected_bytes is None

    seen: list[FetchProgress] = []
    result = await fetcher.fetch(item, tmp_path / "gen.bin", progress=seen.append)

    assert result.bytes_written == size
    assert seen[-1].transfer_total == size


async def test_a_body_with_no_length_at_all_still_produces_the_file(
    server: FixtureServer, tmp_path: Path
) -> None:
    """A host that measures nothing leaves progress total-less, which is a fact and not a crash."""
    size = 200_000
    item = ResolvedFile(
        transfers=(Transfer(url(server, f"unmeasured/{size}")),), filename="unmeasured.bin"
    )
    seen: list[FetchProgress] = []

    result = await fetcher.fetch(item, tmp_path / "unmeasured.bin", progress=seen.append)

    assert result.bytes_written == size
    assert seen[-1].transfer_total is None
    assert seen[-1].overall_total is None


def test_a_reported_content_length_of_zero_is_not_a_size() -> None:
    """The 0-vs-None rule of ``contracts.expected_bytes``: zero is measured, absent is unknown."""

    class Response:
        headers = {"content-length": "0"}

    class Unhelpful:
        headers = {"content-length": "not a number"}

    class NoHeaders:
        pass

    assert fetcher._content_length(Response()) is None
    assert fetcher._content_length(Unhelpful()) is None
    assert fetcher._content_length(NoHeaders()) is None


# ── the running cap that closes D31 ──────────────────────────────────────


async def test_a_transfer_that_passes_the_cap_is_stopped_and_leaves_nothing(
    server: FixtureServer, tmp_path: Path
) -> None:
    destination = tmp_path / "huge.bin"
    item = ResolvedFile(
        transfers=(Transfer(url(server, "unmeasured/5000000")),), filename="huge.bin"
    )

    with pytest.raises(SizeLimitExceeded) as caught:
        await fetcher.fetch(item, destination, limit_bytes=MB)

    assert caught.value.limit_bytes == MB
    assert caught.value.written_bytes > MB
    assert str(caught.value) == catalog.LIMIT_EXCEEDED_REASON.format(limit="1")
    assert not destination.exists()


async def test_exactly_the_cap_is_allowed(server: FixtureServer, tmp_path: Path) -> None:
    """The limit refuses what *exceeds* it, the same reading as the pre-transfer gate (E3)."""
    size = 100_000

    result = await fetcher.fetch(
        generated_item(server, size), tmp_path / "exact.bin", limit_bytes=size
    )

    assert result.bytes_written == size


async def test_the_cap_belongs_to_the_file_not_to_each_transfer(
    server: FixtureServer, tmp_path: Path
) -> None:
    """Otherwise a two-stream video could download twice the limit by arriving in two pieces."""
    item = ResolvedFile(
        transfers=(
            Transfer(url(server, "padded/300000"), "video", 300_000),
            Transfer(url(server, "padded/300000"), "audio", 300_000),
        ),
        filename="capped.mp4",
        media_kind="video",
    )

    with pytest.raises(SizeLimitExceeded) as caught:
        await fetcher.fetch(item, tmp_path / "capped.mp4", limit_bytes=400_000)

    # The first stream fit; the abort happened while the second was being written.
    assert 300_000 < caught.value.written_bytes < 600_000
    assert not (tmp_path / "capped.mp4").exists()
    # Nothing is left behind to be mistaken for a resumable partial (no Range, no resume).
    assert list(tmp_path.iterdir()) == []


# ── failures (FR-9.2, E6) ────────────────────────────────────────────────


async def test_a_zero_byte_body_is_the_empty_file_failure(
    server: FixtureServer, tmp_path: Path
) -> None:
    destination = tmp_path / "empty.bin"

    with pytest.raises(EmptyFile) as caught:
        await fetcher.fetch(
            ResolvedFile(transfers=(Transfer(url(server, "empty")),), filename="empty.bin"),
            destination,
        )

    assert str(caught.value) == catalog.EMPTY_FILE_REASON
    assert not destination.exists()


async def test_a_server_error_names_the_status_and_leaves_nothing(
    server: FixtureServer, tmp_path: Path
) -> None:
    destination = tmp_path / "missing.bin"

    with pytest.raises(FetchError) as caught:
        await fetcher.fetch(
            ResolvedFile(transfers=(Transfer(url(server, "missing")),), filename="missing.bin"),
            destination,
        )

    assert "404" in str(caught.value)
    assert not destination.exists()


async def test_a_stream_that_dies_midway_is_a_failure_and_leaves_nothing(
    server: FixtureServer, tmp_path: Path
) -> None:
    """A short body must not be delivered as if it were whole: curl reports the missing bytes."""
    destination = tmp_path / "broken.bin"

    with pytest.raises(FetchError) as caught:
        await fetcher.fetch(
            ResolvedFile(transfers=(Transfer(url(server, "truncate")),), filename="broken.bin"),
            destination,
        )

    assert str(caught.value).startswith("transfer failed:")
    assert not destination.exists()


async def test_a_host_that_cannot_be_reached_is_a_fetch_error(tmp_path: Path) -> None:
    """Nothing listening means a message, not a traceback escaping into a chat."""
    item = ResolvedFile(transfers=(Transfer("http://127.0.0.1:1/never"),), filename="never.bin")

    with pytest.raises(FetchError) as caught:
        await fetcher.fetch(item, tmp_path / "never.bin")

    assert str(caught.value)


# ── merging two streams into one file (D30, D28) ─────────────────────────


async def test_two_streams_are_merged_into_one_playable_file(
    media_server: FixtureServer, tmp_path: Path
) -> None:
    item = merge_item(media_server)
    seen: list[FetchProgress] = []
    destination = tmp_path / "merged.mp4"
    assert item.expected_bytes == item.transfers[0].filesize + item.transfers[1].filesize

    result = await fetcher.fetch(item, destination, progress=seen.append)

    assert result.merged is True
    assert result.media_kind == "video"
    assert result.bytes_written == destination.stat().st_size > 0
    assert streams_in(destination) == ["audio", "video"]
    # One file, and no evidence of how it was made.
    assert list(tmp_path.glob("*.part*")) == []
    assert [p.name for p in tmp_path.iterdir()] == ["merged.mp4"]


async def test_progress_spans_both_streams_rather_than_restarting(
    media_server: FixtureServer, tmp_path: Path
) -> None:
    """A user watching one file should see one rising number, not two."""
    item = merge_item(media_server)
    seen: list[FetchProgress] = []

    await fetcher.fetch(item, tmp_path / "merged.mp4", progress=seen.append)

    video = item.transfers[0].filesize or 0
    audio = item.transfers[1].filesize or 0
    # The video's own numbers, then the audio's counted on top of it.
    assert seen[0].overall_done == video
    assert seen[-1].role == "audio"
    assert seen[-1].transfer_done == audio
    assert seen[-1].overall_done == video + audio
    assert seen[-1].overall_total == video + audio
    assert [u.overall_done for u in seen] == sorted(u.overall_done for u in seen)


async def test_streams_that_are_not_one_video_and_one_audio_are_refused(
    server: FixtureServer, tmp_path: Path
) -> None:
    """A merge we do not understand is reported, rather than guessed at."""
    item = ResolvedFile(
        transfers=(
            Transfer(url(server, "padded/1000"), "file", 1000),
            Transfer(url(server, "padded/1000"), "file", 1000),
        ),
        filename="confused.mp4",
    )

    with pytest.raises(FetchError, match="cannot merge"):
        await fetcher.fetch(item, tmp_path / "confused.mp4")

    # The refusal happens before any transfer, so nothing was even started.
    assert list(tmp_path.iterdir()) == []


async def test_a_merge_ffmpeg_rejects_is_a_failure_and_leaves_nothing(
    media_server: FixtureServer, tmp_path: Path
) -> None:
    item = merge_item(media_server, audio="corrupt.m4a", filename="broken.mp4")
    destination = tmp_path / "broken.mp4"

    with pytest.raises(FetchError) as caught:
        await fetcher.fetch(item, destination)

    assert "combining the streams failed" in str(caught.value)
    assert not destination.exists()
    assert list(tmp_path.iterdir()) == []


async def test_a_missing_ffmpeg_is_reported_rather_than_crashing(
    media_server: FixtureServer, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every video is a merge, so a missing ffmpeg is a message rather than a crash."""
    monkeypatch.setattr(fetcher.shutil, "which", lambda name: None)
    destination = tmp_path / "merged.mp4"

    with pytest.raises(FetchError, match="ffmpeg") as caught:
        await fetcher.fetch(merge_item(media_server), destination)

    assert "required" in str(caught.value)
    assert list(tmp_path.iterdir()) == []


# ── HLS: a manifest is not a file (the reason `hls` is a transfer role) ───


def hls_item(server: FixtureServer, playlist: str, **overrides: object) -> ResolvedFile:
    """An item whose one transfer is a playlist, which is what a native adapter hands back."""
    fields: dict[str, object] = {
        "transfers": (Transfer(url(server, f"file/{playlist}"), "hls"),),
        "filename": "stream.mp4",
        "media_kind": "video",
    }
    fields.update(overrides)
    return ResolvedFile(**fields)  # type: ignore[arg-type]


@pytest.fixture
def hls(server: FixtureServer, media: Path) -> FixtureServer:
    """The fixture server, with a real HLS playlist and segment under ``hls/``.

    Generated rather than hand-written: a synthetic playlist would let a wrong implementation pass,
    because only a real demuxer can say whether what came out is playable.
    """
    if shutil.which("ffmpeg") is None:
        pytest.skip("ffmpeg is required to produce an HLS stream")
    directory = server.root / "hls"
    directory.mkdir(parents=True, exist_ok=True)
    _ffmpeg(
        "-i", str(media / "video.mp4"),
        "-c", "copy",
        "-f", "hls",
        "-hls_time", "1",
        "-hls_list_size", "0",
        "-hls_segment_filename", str(directory / "seg%d.ts"),
        str(directory / "index.m3u8"),
    )
    return server


async def test_an_hls_item_arrives_as_a_playable_file(hls: FixtureServer, tmp_path: Path) -> None:
    """The manifest is walked by ffmpeg, so what lands is media rather than the playlist text."""
    destination = tmp_path / "stream.mp4"

    result = await fetcher.fetch(hls_item(hls, "hls/index.m3u8"), destination)

    assert result.bytes_written > 0
    assert result.merged is True, "an HLS file came out of ffmpeg, not out of a plain download"
    assert streams_in(destination) == ["video"]


async def test_a_playlist_fetched_as_a_plain_file_is_refused(
    hls: FixtureServer, staging: Path
) -> None:
    """The bug the ``hls`` role exists to prevent, caught at the only place that can see it.

    A bridge can hand back a fresh ``.m3u8`` URL under a name no resolver classified as a playlist,
    so the role alone is not enough: the response's own bytes are checked when it arrives. Reporting
    a failure is the honest outcome — the alternative is delivering a text file named ``.mp4`` that
    the user sees as a broken video.
    """
    destination = staging / "plain.mp4"

    with pytest.raises(FetchError, match="HLS playlist") as caught:
        await fetcher.fetch(
            ResolvedFile(
                transfers=(Transfer(url(hls, "file/hls/index.m3u8"), "file"),),
                filename="plain.mp4",
            ),
            destination,
        )

    assert "hls transfer" in str(caught.value), "the message says what to do about it"
    assert not destination.exists(), "the playlist was not left on disk as though it were media"


async def test_an_hls_download_reports_bytes_without_a_percentage(
    hls: FixtureServer, tmp_path: Path
) -> None:
    """D30: no ``Content-Length`` exists for a playlist, so no total is invented."""
    seen: list[FetchProgress] = []

    async def record(update: FetchProgress) -> None:
        seen.append(update)

    await fetcher.fetch(hls_item(hls, "hls/index.m3u8"), tmp_path / "stream.mp4", progress=record)

    assert seen, "the transfer reported something"
    assert all(update.overall_total is None for update in seen)
    assert seen[-1].overall_done > 0


async def test_the_cap_refuses_an_hls_stream_that_is_too_large(
    hls: FixtureServer, tmp_path: Path
) -> None:
    """D31: with no size up front, the running cap is the only enforcement there is."""
    destination = tmp_path / "stream.mp4"

    with pytest.raises(SizeLimitExceeded) as caught:
        await fetcher.fetch(hls_item(hls, "hls/index.m3u8"), destination, limit_bytes=1)

    assert caught.value.written_bytes > 1
    assert not destination.exists(), "the oversized partial is discarded"


async def test_a_cancelled_hls_download_kills_ffmpeg_and_leaves_nothing(
    hls: FixtureServer, staging: Path
) -> None:
    """FR-11.6: the child must be killed, or it keeps pulling segments we just refused."""
    destination = staging / "stream.mp4"

    with pytest.raises(TransferCancelled):
        await fetcher.fetch(
            hls_item(hls, "hls/index.m3u8"),
            destination,
            should_stop=lambda: True,
        )

    assert not destination.exists()
    assert list(staging.iterdir()) == [], "no partial survives a cancellation"


async def test_a_broken_playlist_is_a_failure_rather_than_an_empty_file(
    hls: FixtureServer, staging: Path
) -> None:
    (hls.root / "hls" / "broken.m3u8").write_bytes(b"#EXTM3U\n#EXTINF:1,\nmissing.ts\n")
    destination = staging / "stream.mp4"

    with pytest.raises(FetchError) as caught:
        await fetcher.fetch(hls_item(hls, "hls/broken.m3u8"), destination)

    assert "HLS stream could not be downloaded" in str(caught.value)
    assert list(staging.iterdir()) == [], "a failed conversion leaves no partial behind"


async def test_an_hls_item_without_ffmpeg_says_so(
    hls: FixtureServer, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(fetcher.shutil, "which", lambda name: None)

    with pytest.raises(FetchError, match="ffmpeg"):
        await fetcher.fetch(hls_item(hls, "hls/index.m3u8"), tmp_path / "stream.mp4")


# ── the chain guard: nobody hands the fetcher a playlist it will not consume ──


def test_no_adapter_asks_for_a_playlist_to_be_saved_as_a_file() -> None:
    """A ``.m3u8`` transfer must carry the ``hls`` role.

    This is the regression guard for the bug that motivated the role: three ported adapters returned
    a playlist as ``role="video"``, which the fetcher would have written to disk as a text file
    named ``.mp4``. The check reads the adapters themselves, so a new one cannot reintroduce it and
    pass because its unit tests happened to assert nothing about the role.

    Only this direction is checked. The opposite (an ``hls`` role on a direct URL) is not a bug —
    ffmpeg reads a plain file just as happily — and one adapter legitimately does both from one code
    path when a bridge may answer with either.
    """
    package = pathlib.Path(fetcher.__file__).parent / "sites"
    offenders: list[str] = []

    for path in sorted(package.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = getattr(node.func, "attr", None) or getattr(node.func, "id", None)
            if name != "media":
                continue
            arguments = dict(zip(("url", "filename"), node.args, strict=False))
            arguments.update({keyword.arg: keyword.value for keyword in node.keywords})
            if _is_playlist_expression(arguments.get("url")) and (
                _role_of(arguments.get("role")) != "hls"
            ):
                offenders.append(f"{path.name}: {ast.unparse(node)}")

    assert offenders == [], f"a playlist would be saved as a file in: {offenders}"


def _role_of(node: ast.expr | None) -> str:
    """The literal role passed to ``media``, defaulting to the ``file`` that ``media`` uses."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return "file"


def _is_playlist_expression(node: ast.expr | None) -> bool:
    """Whether the URL handed to ``media`` is a resolved HLS playlist.

    Two shapes are accepted, because both appear in the adapters: a variable whose *name* says so
    (``hls_url``), and a literal ``.m3u8``. A name-based read is coarse, which is the point — it
    fails loudly on the naming an adapter would use if it were handling a playlist.
    """
    if node is None:
        return False
    if isinstance(node, ast.Name):
        return "hls" in node.id.lower() or "playlist" in node.id.lower()
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return ".m3u8" in node.value
    return False
