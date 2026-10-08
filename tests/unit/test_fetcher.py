"""Fetching the bytes ourselves (D25, D31, FR-7): streaming, merging, and the failures that matter.

Every test here talks to a real HTTP server on loopback and writes real files, because the
behaviour under test *is* the transport: whether the headers arrive, whether a cap stops a transfer
mid-flight, whether what ffmpeg produced is playable. A stubbed transport would assert what I
believe curl_cffi does rather than what it does — and the merge itself can only be verified by a
real demuxer reading the result.
"""

from __future__ import annotations

import shutil
import subprocess
import threading
import time
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
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
)

MB = 1024 * 1024


# ── a real HTTP server ───────────────────────────────────────────────────
#
# HTTP/1.0 on purpose: every response ends by closing the connection, so a body with no
# Content-Length is delimited by the close, which is exactly how a media host that refuses to
# measure itself behaves.


class _Server(ThreadingHTTPServer):
    daemon_threads = True

    #: Where ``/file/...`` is served from.
    root: Path
    #: Every request's headers, in arrival order, so a test can assert what actually went out.
    requests: list[dict[str, str]]


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"

    def log_message(self, *args: object) -> None:
        """Silence the default stderr logging. A test run is not a server log."""

    def do_GET(self) -> None:  # noqa: N802 - the name is the protocol
        server: _Server = self.server  # type: ignore[assignment]
        server.requests.append(dict(self.headers.items()))

        route, _, argument = self.path.lstrip("/").partition("/")
        try:
            if route == "file":
                self._send_bytes(200, (server.root / argument).read_bytes())
            elif route == "padded":
                self._send_generated(int(argument), measured=True)
            elif route == "unmeasured":
                self._send_generated(int(argument), measured=False)
            elif route == "empty":
                self._send_bytes(200, b"")
            elif route == "missing":
                self._send_bytes(404, b"gone")
            elif route == "truncate":
                self._truncate()
            elif route == "slow":
                self._slow(int(argument))
            else:
                self._send_bytes(404, b"unknown route")
        except BrokenPipeError, ConnectionResetError:
            # Expected whenever a test asserts that the transfer was stopped early.
            self.close_connection = True

    def _send_bytes(self, status: int, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_generated(self, size: int, *, measured: bool) -> None:
        """A body of ``size`` bytes, with or without a Content-Length to announce it."""
        self.send_response(200)
        if measured:
            self.send_header("Content-Length", str(size))
        self.end_headers()
        block = b"z" * 65536
        remaining = size
        while remaining > 0:
            piece = block[: min(len(block), remaining)]
            self.wfile.write(piece)
            remaining -= len(piece)

    def _truncate(self) -> None:
        """Promise more than is sent, then hang up: a stream that dies mid-body."""
        self.send_response(200)
        self.send_header("Content-Length", "10000")
        self.end_headers()
        self.wfile.write(b"y" * 100)
        self.wfile.flush()
        self.close_connection = True

    def _slow(self, size: int) -> None:
        """Send half now and half after a pause, so a client can be caught mid-transfer."""
        half = size // 2
        self.send_response(200)
        self.send_header("Content-Length", str(size))
        self.end_headers()
        self.wfile.write(b"s" * half)
        self.wfile.flush()
        time.sleep(0.4)
        self.wfile.write(b"s" * (size - half))


@pytest.fixture
def server(tmp_path: Path) -> Iterator[_Server]:
    httpd = _Server(("127.0.0.1", 0), _Handler)
    httpd.root = tmp_path
    httpd.requests = []
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield httpd
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


def url(server: _Server, path: str) -> str:
    """The loopback URL for a route, so no host other than 127.0.0.1 is ever contacted."""
    host, port = server.server_address[0], server.server_address[1]
    return f"http://{host}:{port}/{path}"


def file_item(server: _Server, name: str, **overrides: object) -> ResolvedFile:
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
def media_server(server: _Server, media: Path) -> _Server:
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
    server: _Server,
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


def generated_item(server: _Server, size: int, *, filename: str = "generated.bin") -> ResolvedFile:
    """One transfer of ``size`` bytes with no size reported by any extractor."""
    return ResolvedFile(
        transfers=(Transfer(url(server, f"padded/{size}")),),
        filename=filename,
    )


# ── one transfer ─────────────────────────────────────────────────────────


async def test_a_single_transfer_is_streamed_to_the_destination(
    server: _Server, tmp_path: Path
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


async def test_the_destination_directory_is_created(server: _Server, tmp_path: Path) -> None:
    """The staging directory may not exist yet, and fetching should not assume it does."""
    (server.root / "payload.bin").write_bytes(b"abc")

    result = await fetcher.fetch(file_item(server, "payload.bin"), tmp_path / "a" / "b" / "out.bin")

    assert result.path.read_bytes() == b"abc"


async def test_browser_headers_are_sent_and_impersonation_supplies_a_user_agent(
    server: _Server, tmp_path: Path
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
    server: _Server, tmp_path: Path
) -> None:
    """A caller-owned session outlives the fetch: one pool per worker, not per file."""
    (server.root / "payload.bin").write_bytes(b"abc")

    async with requests.AsyncSession() as client:
        await fetcher.fetch(file_item(server, "payload.bin"), tmp_path / "out.bin", session=client)
        response = await client.get(url(server, "empty"), impersonate="chrome", timeout=10)

    assert response.status_code == 200


# ── progress (FR-7.4) ────────────────────────────────────────────────────


async def test_progress_is_reported_in_steps_and_ends_at_the_total(
    server: _Server, tmp_path: Path
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
    server: _Server, tmp_path: Path
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


async def test_a_plain_function_works_as_a_progress_hook(server: _Server, tmp_path: Path) -> None:
    """Not every caller wants a coroutine, and a hook that returns None must not be awaited."""
    seen: list[FetchProgress] = []

    await fetcher.fetch(generated_item(server, 4096), tmp_path / "gen.bin", progress=seen.append)

    assert seen[-1].transfer_done == 4096


async def test_a_size_the_extractor_never_reported_is_taken_from_the_server(
    server: _Server, tmp_path: Path
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
    server: _Server, tmp_path: Path
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
    server: _Server, tmp_path: Path
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


async def test_exactly_the_cap_is_allowed(server: _Server, tmp_path: Path) -> None:
    """The limit refuses what *exceeds* it, the same reading as the pre-transfer gate (E3)."""
    size = 100_000

    result = await fetcher.fetch(
        generated_item(server, size), tmp_path / "exact.bin", limit_bytes=size
    )

    assert result.bytes_written == size


async def test_the_cap_belongs_to_the_file_not_to_each_transfer(
    server: _Server, tmp_path: Path
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


async def test_a_zero_byte_body_is_the_empty_file_failure(server: _Server, tmp_path: Path) -> None:
    destination = tmp_path / "empty.bin"

    with pytest.raises(EmptyFile) as caught:
        await fetcher.fetch(
            ResolvedFile(transfers=(Transfer(url(server, "empty")),), filename="empty.bin"),
            destination,
        )

    assert str(caught.value) == catalog.EMPTY_FILE_REASON
    assert not destination.exists()


async def test_a_server_error_names_the_status_and_leaves_nothing(
    server: _Server, tmp_path: Path
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
    server: _Server, tmp_path: Path
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
    media_server: _Server, tmp_path: Path
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
    media_server: _Server, tmp_path: Path
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
    server: _Server, tmp_path: Path
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
    media_server: _Server, tmp_path: Path
) -> None:
    item = merge_item(media_server, audio="corrupt.m4a", filename="broken.mp4")
    destination = tmp_path / "broken.mp4"

    with pytest.raises(FetchError) as caught:
        await fetcher.fetch(item, destination)

    assert "combining the streams failed" in str(caught.value)
    assert not destination.exists()
    assert list(tmp_path.iterdir()) == []


async def test_a_missing_ffmpeg_is_reported_rather_than_crashing(
    media_server: _Server, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every video is a merge, so a missing ffmpeg is a message rather than a crash."""
    monkeypatch.setattr(fetcher.shutil, "which", lambda name: None)
    destination = tmp_path / "merged.mp4"

    with pytest.raises(FetchError, match="ffmpeg") as caught:
        await fetcher.fetch(merge_item(media_server), destination)

    assert "required" in str(caught.value)
    assert list(tmp_path.iterdir()) == []
