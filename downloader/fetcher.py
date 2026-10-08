"""Fetching the bytes ourselves, and merging what needs merging (D25, D31, FR-7).

The engines resolve; they do not transfer (D25). Doing it here is what gives one progress
vocabulary for every host, our own size accounting for the quota, and a size gate that works on
what was measured rather than on what an engine chose to do.

Three behaviours here are decisions rather than details:

* **No Range, no resume.** The spec never asks for it, and the ledger that makes a restart safe
  (`job_items`) exists to prevent re-*delivery*, not re-download. A job whose lease expired simply
  fetches again, which is cheap to reason about; a resumed partial file is not, because nothing
  verifies that the bytes already on disk are the ones the server would have sent.
* **Per-host headers live in code.** A `Referer` or `User-Agent` a host demands is a fact about
  that host, not operator policy, so a config key would only offer a way to get it wrong. Cookies
  are the exception: those belong to a user and live in their own file (D8).
* **The running cap closes D31.** When resolution could not report a size the transfer is allowed
  but watched, and aborted the moment the running total passes the limit — so the tier's limit
  holds whether or not the extractor measured it.

Every transfer is streamed to disk as it arrives. Nothing is buffered whole: a 4 GB share would
otherwise sit in memory before being written.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from curl_cffi import requests

from app import catalog
from downloader.contracts import MediaKind, ResolvedFile, Transfer
from downloader.gate import format_mb

log = logging.getLogger(__name__)

#: Progress is reported at most once per this many bytes, so a fast host cannot produce thousands
#: of callbacks a second. The status layer coalesces on its own interval anyway (FR-7.7).
#:
#: Note this is *our* reporting interval, not a read size: curl_cffi ignores ``chunk_size``
#: (measured — it warns) and yields whatever libcurl hands over. That is still incremental
#: delivery, which is what keeps a 4 GB share out of memory; the bytes per chunk are simply not
#: ours to choose.
PROGRESS_STEP_BYTES = 256 * 1024

DEFAULT_TIMEOUT = 120.0

#: A browser fingerprint, because several hosts serve a 403 to a plain Python user agent. curl_cffi
#: impersonates a real client at the TLS level, which is why it was chosen over aiohttp (R13).
DEFAULT_IMPERSONATE = "chrome"

BROWSER_HEADERS: dict[str, str] = {
    "Accept": "*/*",
    "Accept-Language": "en-US,en;q=0.9",
}

#: Per-host requirements, added to the browser defaults. Grows only when a host is observed to
#: demand something: the media hosts of a video site are not the same host as the page it resolved
#: from, which is why `googlevideo.com` appears here alongside `youtube.com`.
HOST_HEADERS: dict[str, dict[str, str]] = {
    "youtube.com": {"Referer": "https://www.youtube.com/"},
    "youtu.be": {"Referer": "https://www.youtube.com/"},
    "googlevideo.com": {"Referer": "https://www.youtube.com/"},
    "twitter.com": {"Referer": "https://twitter.com/"},
    "x.com": {"Referer": "https://x.com/"},
    "reddit.com": {"Referer": "https://www.reddit.com/"},
    "redd.it": {"Referer": "https://www.reddit.com/"},
    "instagram.com": {"Referer": "https://www.instagram.com/"},
    "tiktok.com": {"Referer": "https://www.tiktok.com/"},
}


class FetchError(RuntimeError):
    """A transfer failed. The message is fit to show a user (FR-9.2)."""


class SizeLimitExceeded(FetchError):
    """A transfer passed the tier's limit mid-flight, having reported no size up front (D31)."""

    def __init__(self, limit_bytes: int, written_bytes: int) -> None:
        super().__init__(
            catalog.LIMIT_EXCEEDED_REASON.format(limit=format_mb(limit_bytes)),
        )
        self.limit_bytes = limit_bytes
        #: What had already been written when the cap was passed. Reported because it is the band's
        #: used-bandwidth accounting: those bytes really did come off the network.
        self.written_bytes = written_bytes


class EmptyFile(FetchError):
    """A transfer produced nothing at all (E6)."""

    def __init__(self) -> None:
        super().__init__(catalog.EMPTY_FILE_REASON)


@dataclass(frozen=True)
class FetchProgress:
    """Where a transfer has got to, for the status layer and for quota accounting (FR-7.4)."""

    url: str
    role: str
    transfer_done: int
    transfer_total: int | None
    overall_done: int
    overall_total: int | None


ProgressHook = Callable[[FetchProgress], Awaitable[None] | None]


@dataclass(frozen=True)
class FetchedFile:
    """A file on disk, ready to be delivered."""

    path: Path
    media_kind: MediaKind
    bytes_written: int
    source_url: str | None = None
    note: str | None = None
    #: True when this file came out of ffmpeg rather than being a straight download.
    merged: bool = False


def headers_for(url: str) -> dict[str, str]:
    """Browser defaults plus anything this host requires."""
    host = (urlparse(url).hostname or "").lower()
    headers = dict(BROWSER_HEADERS)
    for marker, extra in HOST_HEADERS.items():
        if host == marker or host.endswith("." + marker):
            headers.update(extra)
            break
    return headers


async def fetch(
    item: ResolvedFile,
    destination: Path,
    *,
    limit_bytes: int | None = None,
    progress: ProgressHook | None = None,
    session: Any | None = None,
    timeout: float = DEFAULT_TIMEOUT,
    impersonate: str | None = DEFAULT_IMPERSONATE,
) -> FetchedFile:
    """Produce ``destination`` from ``item``'s transfers, merging if there is more than one.

    ``limit_bytes`` is the running cap from D31 and is only needed when the size could not be
    determined up front; passing it always is harmless, since a known oversize never gets here
    (the gate refused it before any transfer started).
    """
    destination.parent.mkdir(parents=True, exist_ok=True)
    owns_session = session is None
    client = session or requests.AsyncSession()

    try:
        if len(item.transfers) == 1:
            written = await _download_one(
                client,
                item.transfers[0],
                destination,
                limit_bytes=limit_bytes,
                progress=progress,
                timeout=timeout,
                impersonate=impersonate,
                base_total=item.expected_bytes,
                sole_transfer=True,
            )
            merged = False
        else:
            written, merged = await _download_and_merge(
                client,
                item,
                destination,
                limit_bytes=limit_bytes,
                progress=progress,
                timeout=timeout,
                impersonate=impersonate,
            )
    finally:
        if owns_session:
            await client.close()

    if written <= 0:
        _discard(destination)
        raise EmptyFile()

    return FetchedFile(
        path=destination,
        media_kind=item.media_kind,
        bytes_written=written,
        source_url=item.source_url,
        note=item.note,
        merged=merged,
    )


async def _download_and_merge(
    client: Any,
    item: ResolvedFile,
    destination: Path,
    *,
    limit_bytes: int | None,
    progress: ProgressHook | None,
    timeout: float,
    impersonate: str | None,
) -> tuple[int, bool]:
    """Fetch both streams, then combine them with ffmpeg."""
    video = next((t for t in item.transfers if t.role == "video"), None)
    audio = next((t for t in item.transfers if t.role == "audio"), None)
    if video is None or audio is None:
        # Not a merge we understand. Better to say so than to guess which stream is which.
        raise FetchError("cannot merge: the streams are not one video and one audio")

    video_path = destination.with_name(destination.name + ".part0")
    audio_path = destination.with_name(destination.name + ".part1")
    parts = [video_path, audio_path]
    try:
        video_bytes = await _download_one(
            client,
            video,
            video_path,
            limit_bytes=limit_bytes,
            progress=progress,
            timeout=timeout,
            impersonate=impersonate,
            base_total=item.expected_bytes,
        )
        await _download_one(
            client,
            audio,
            audio_path,
            limit_bytes=limit_bytes,
            progress=progress,
            timeout=timeout,
            impersonate=impersonate,
            # The file's progress is both streams, so the second one continues the first
            # rather than restarting the count the user is watching.
            base_done=video_bytes,
            base_total=item.expected_bytes,
        )
        bytes_written = await _merge(video_path, audio_path, destination)
    finally:
        for part in parts:
            _discard(part)

    return bytes_written, True


async def _download_one(
    client: Any,
    transfer: Transfer,
    path: Path,
    *,
    limit_bytes: int | None,
    progress: ProgressHook | None,
    timeout: float,
    impersonate: str | None,
    base_done: int = 0,
    base_total: int | None = None,
    sole_transfer: bool = False,
) -> int:
    """Stream one transfer to ``path``. Returns the bytes written."""
    written = 0
    reported = 0
    expected = transfer.filesize

    try:
        response = await client.get(
            transfer.url,
            headers=headers_for(transfer.url),
            timeout=timeout,
            stream=True,
            impersonate=impersonate,
        )
    except Exception as error:  # noqa: BLE001 - the transport's message is the useful one
        raise FetchError(str(error).strip() or "the transfer could not be started") from error

    try:
        if response.status_code >= 400:
            raise FetchError(f"the server answered {response.status_code}")
        if expected is None:
            expected = _content_length(response)
        # A single-transfer file *is* its stream, so its total is whatever we just learned. In a
        # merge the file's total is only known when every stream reported one (D30), and a partial
        # sum would be a lie told in the shape of a precise number.
        overall_total = expected if sole_transfer else base_total

        with path.open("wb") as handle:
            async for chunk in response.aiter_content():
                if not chunk:
                    continue
                handle.write(chunk)
                written += len(chunk)

                # D31's running cap is the *file's* cap, not each stream's: base_done is what
                # earlier streams of the same file already wrote, so a merge cannot slip past the
                # limit by arriving in two pieces.
                if limit_bytes is not None and base_done + written > limit_bytes:
                    raise SizeLimitExceeded(limit_bytes, base_done + written)

                if progress is not None and written - reported >= PROGRESS_STEP_BYTES:
                    reported = written
                    await _emit(progress, transfer, written, expected, base_done, overall_total)

        if progress is not None:
            await _emit(progress, transfer, written, expected, base_done, overall_total)
    except FetchError:
        _discard(path)
        raise
    except Exception as error:  # noqa: BLE001 - a dropped connection must name the URL
        _discard(path)
        raise FetchError(f"transfer failed: {error}") from error
    finally:
        await _close(response)

    return written


async def _close(response: Any) -> None:
    """Release the connection, whichever close method this client version offers."""
    for name in ("aclose", "close"):
        closer = getattr(response, name, None)
        if closer is None:
            continue
        result = closer()
        if isinstance(result, Awaitable):
            await result
        return


async def _emit(
    progress: ProgressHook,
    transfer: Transfer,
    written: int,
    expected: int | None,
    base_done: int,
    overall_total: int | None,
) -> None:
    """Report progress, tolerating a hook that is a plain function rather than a coroutine."""
    update = FetchProgress(
        url=transfer.url,
        role=transfer.role,
        transfer_done=written,
        transfer_total=expected,
        overall_done=base_done + written,
        overall_total=overall_total,
    )
    result = progress(update)
    if isinstance(result, Awaitable):
        await result


def _content_length(response: Any) -> int | None:
    """The server's own Content-Length, used when the extractor did not report a size."""
    headers = getattr(response, "headers", None)
    if headers is None:
        return None
    raw = headers.get("content-length") or headers.get("Content-Length")
    try:
        value = int(raw)
    except TypeError, ValueError:
        return None
    return value if value > 0 else None


async def _merge(video: Path, audio: Path, destination: Path) -> int:
    """Combine two streams into one file without re-encoding.

    ``-c copy`` keeps the merge to a demux/remux rather than a transcode, which is both instant and
    lossless — the compatibility decision was already made when the streams were chosen (D28).
    ``+faststart`` moves the index to the front of an MP4, which is what lets Telegram begin
    playing without fetching the whole file first.
    """
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        raise FetchError("ffmpeg is required to combine video and audio, and was not found")

    command = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        str(video),
        "-i",
        str(audio),
        "-c",
        "copy",
    ]
    if destination.suffix.lower() == ".mp4":
        command += ["-movflags", "+faststart"]
    command.append(str(destination))

    process = await asyncio.create_subprocess_exec(
        *command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await process.communicate()

    if process.returncode != 0 or not destination.exists():
        _discard(destination)
        detail = stderr.decode("utf-8", "replace").strip().splitlines()
        raise FetchError(
            f"combining the streams failed: {detail[-1][:200] if detail else 'unknown error'}"
        )

    size = destination.stat().st_size
    if size <= 0:
        _discard(destination)
        raise EmptyFile()
    log.debug("merged %s + %s into %s (%d bytes)", video.name, audio.name, destination.name, size)
    return size


def _discard(path: Path) -> None:
    """Remove a partial file. Absent is the normal case, so it is not an error."""
    try:
        path.unlink(missing_ok=True)
    except OSError:  # pragma: no cover - a locked file on Windows; the caller reports the failure
        log.warning("could not remove the partial file %s", path)
