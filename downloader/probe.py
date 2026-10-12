"""Turning a link into transfers: engine routing, invocation, and parsing (FR-4, D5, D25).

Two engines, two output shapes, one contract (:mod:`downloader.contracts`). Everything here is
written against **observed** output rather than documentation, because the documentation is wrong in
ways that would silently break a parser:

* ``gallery-dl -j`` prints one pretty-printed JSON **array**, not one object per line, and its
  records are **typed and positional** — ``[3, "<url>", {meta}]`` for a file, ``[2, {meta}]`` for a
  directory, ``[-1, {error, message}]`` for a failure. The URL is not a dict key.
* ``gallery-dl`` **exits 0 when it resolved nothing** (a 404 arrives as exit 0 plus a ``[-1]``
  record), so the exit code is never treated as a success signal here.
* For yt-dlp-backed items its URL slot holds a ``ytdl:https://…`` **marker**, not media. The real,
  time-limited URLs live in ``metadata["formats"]`` / ``metadata["requested_formats"]``, which is
  why those items are re-selected through :mod:`downloader.selection` instead of being fetched from
  the marker.
* ``yt-dlp`` prints **one JSON object**, and its top-level dict can have **no ``url``** at all when
  the chosen pair is a merge.

Resolution is kept separate from transfer (D25): this module never downloads. It also never
fetches something it cannot fetch — an item with no URL is not a candidate, however good it looks.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlparse

from downloader.contracts import (
    MediaKind,
    Resolution,
    ResolvedFile,
    ResolveError,
    Transfer,
    kind_for,
)
from downloader.selection import Preference, choose, from_formats

log = logging.getLogger(__name__)

Engine = Literal["gallerydl", "ytdlp"]

#: Hosts the yt-dlp *library* resolves directly (D5). gallery-dl can reach these too, but only by
#: handing back a marker that carries no media, so the direct route is both simpler and more
#: honest about what happened.
YOUTUBE_HOSTS = ("youtube.com", "youtu.be", "youtube-nocookie.com")

#: gallery-dl's ytdl extractor is **off by default**: without this flag YouTube comes back as
#: "Unsupported URL" with exit 64. It is passed on every invocation rather than configured in a
#: file so that the behaviour cannot depend on a user's own gallery-dl config.
YTDL_EXTRACTOR_FLAG = "extractor.ytdl.enabled=true"

DEFAULT_RESOLVE_TIMEOUT = 180.0
DEFAULT_FILENAME = "download"

#: Record codes gallery-dl emits. Only 3 carries media; the rest are structure or errors.
RECORD_DIRECTORY = 2
RECORD_FILE = 3
RECORD_ERROR = -1

#: A runner is injectable so a test can drive the parsers without a subprocess. The keyword set is
#: ``(url, timeout=…, cookies_file=…, extractor_options=…)``, all optional beyond the URL.
GallerydlRunner = Callable[..., Awaitable[str]]
YtdlpRunner = Callable[..., Awaitable[Mapping[str, Any]]]

#: Where the gallery-dl executable is. Overridable so a container can point elsewhere.
GALLERYDL_EXECUTABLE = "gallery-dl"


def engine_for(url: str) -> Engine:
    """Which resolver handles this link.

    Deliberately a plain host check: it decides *resolution*, and the native per-host adapters
    (TeraBox, the Cyberdrop family, the ad-gated hosts) are a separate concern that
    ``downloader.registry`` owns.
    """
    return "ytdlp" if is_video_host(url) else "gallerydl"


def host_of(url: str) -> str:
    """The lower-cased host, without ``www.``, so a rule never has to name both spellings."""
    return _host_of(url).removeprefix("www.")


def is_video_host(url: str) -> bool:
    """Whether the yt-dlp library resolves this host directly (D5)."""
    host = host_of(url)
    return any(host == marker or host.endswith("." + marker) for marker in YOUTUBE_HOSTS)


def gallerydl_command(
    url: str,
    *,
    executable: str = GALLERYDL_EXECUTABLE,
    cookies_file: Path | None = None,
    extractor_options: Mapping[str, str] | None = None,
) -> list[str]:
    """The gallery-dl invocation, as a list so nothing is interpolated through a shell.

    ``--simulate`` is the flag every probe behind this module was validated with, and it is what
    guarantees resolution cannot write files. ``-o`` carries the ytdl enablement rather than a
    config file, so the behaviour cannot depend on the operator's own gallery-dl setup.

    ``--cookies`` is how a user's own cookie file reaches a host that needs one (D8, FR-14). It is
    passed only when the caller resolved a file, because gallery-dl fails on a path that is absent.

    ``extractor_options`` is how a *credential* reaches an extractor — Reddit's client id, for
    instance — again as ``-o`` rather than through a config file, for the same reason: an option on
    the command line cannot be shadowed by whatever the operator has in ``~/.config/gallery-dl``.
    """
    command = [executable, "-j", "--simulate", "-o", YTDL_EXTRACTOR_FLAG]
    for key, value in (extractor_options or {}).items():
        command += ["-o", f"{key}={value}"]
    if cookies_file is not None:
        command += ["--cookies", str(cookies_file)]
    return [*command, url]


def _host_of(url: str) -> str:
    return (urlparse(url).hostname or "").lower()


# ── gallery-dl output ───────────────────────────────────────────────────


@dataclass(frozen=True)
class ParseResult:
    """What a gallery-dl run yielded, and — when it yielded nothing — which kind of nothing.

    ``empty`` is the distinction the user's message depends on: a share that holds no media is E4,
    while an extractor that answered with an error is E2 and its reason is worth repeating.
    """

    files: tuple[ResolvedFile, ...] = ()
    reason: str | None = None
    empty: bool = False


def parse_gallerydl(
    stdout: str,
    *,
    preference: Preference = "compatible",
    source_url: str | None = None,
) -> ParseResult:
    """Parse a gallery-dl run into files, plus a reason when there are none.

    Returns rather than raises, because "no files" is an ordinary outcome that the caller has to
    report with a reason (E4) — and because the reason is sometimes the extractor's own, which is
    more useful to a user than anything this layer could invent.
    """
    text = stdout.strip()
    if not text:
        return ParseResult(reason="the extractor returned nothing", empty=True)

    try:
        records = json.loads(text)
    except json.JSONDecodeError as error:
        return ParseResult(reason=f"the extractor produced unreadable output ({error.msg})")

    if not isinstance(records, list):
        return ParseResult(reason="the extractor produced an unexpected shape")

    files: list[ResolvedFile] = []
    error_message: str | None = None

    for record in records:
        if not isinstance(record, list) or not record:
            continue

        code = record[0]
        if code == RECORD_ERROR:
            error_message = _error_message(record)
            continue
        if code != RECORD_FILE:
            # 2 is a directory/header: structure, not media.
            continue

        url = record[1] if len(record) > 1 else None
        metadata = record[2] if len(record) > 2 and isinstance(record[2], dict) else {}
        if not isinstance(url, str) or not url:
            continue

        item = _file_from_record(url, metadata, preference=preference, source_url=source_url)
        if item is not None:
            files.append(item)

    if files:
        return ParseResult(files=tuple(files))
    if error_message is not None:
        # The extractor named a failure, so this is E2 rather than an empty share.
        return ParseResult(reason=error_message)
    return ParseResult(reason="no downloadable media found at that link", empty=True)


def _error_message(record: list[Any]) -> str:
    payload = record[1] if len(record) > 1 and isinstance(record[1], dict) else {}
    message = payload.get("message") or payload.get("error")
    if isinstance(message, str) and message:
        # The extractor's own wording, e.g. "'404 Not Found' for 'https://…'".
        return message.strip().strip("'")
    return "the extractor reported a failure"


def _file_from_record(
    url: str,
    metadata: Mapping[str, Any],
    *,
    preference: Preference,
    source_url: str | None,
) -> ResolvedFile | None:
    """One ``[3]`` record, or ``None`` when it cannot be fetched."""
    if url.startswith("ytdl:"):
        # gallery-dl delegates to yt-dlp for these and hands back a marker instead of media, so the
        # URLs have to come from the info dict it embedded in the metadata (R13).
        return _file_from_ytdlp_metadata(metadata, preference=preference, source_url=source_url)

    extension = _text(metadata.get("extension") or metadata.get("ext"))
    filename = _filename_from(metadata, extension)
    return ResolvedFile(
        transfers=(Transfer(url=url, filesize=_size(metadata)),),
        filename=filename,
        media_kind=kind_for(extension),
        filesize=_size(metadata),
        source_url=source_url,
    )


def _filename_from(metadata: Mapping[str, Any], extension: str | None) -> str:
    stem = _text(metadata.get("filename") or metadata.get("title")) or DEFAULT_FILENAME
    return f"{stem}.{extension}" if extension else stem


def _text(value: Any) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _size(metadata: Mapping[str, Any]) -> int | None:
    """A reported size, including a reported zero.

    Zero is accepted rather than filtered out: a zero-byte source is a fact the pipeline has a
    specific message for (E6), and dropping it would report the size as unknown instead.
    """
    for key in ("filesize", "size", "filesize_approx"):
        value = metadata.get(key)
        if isinstance(value, (int, float)) and value >= 0:
            return int(value)
    return None


# ── yt-dlp output ───────────────────────────────────────────────────────


def files_from_ytdlp(
    info: Mapping[str, Any],
    *,
    preference: Preference = "compatible",
    source_url: str | None = None,
) -> list[ResolvedFile]:
    """Turn one yt-dlp info dict into the file(s) to fetch.

    An info dict describes **one** video (a playlist is one entry per video), so this returns a
    single-item list in practice; the shape is kept general because ``entries`` exists for
    playlists and a future caller may flatten them.
    """
    entries = info.get("entries")
    if isinstance(entries, list):
        collected: list[ResolvedFile] = []
        for entry in entries:
            if isinstance(entry, dict):
                collected.extend(
                    files_from_ytdlp(entry, preference=preference, source_url=source_url)
                )
        return collected

    return [item] if (item := _file_from_ytdlp_info(info, preference, source_url)) else []


def _file_from_ytdlp_info(
    info: Mapping[str, Any],
    preference: Preference,
    source_url: str | None,
) -> ResolvedFile | None:
    streams = from_formats(info.get("formats") or [])
    selection = choose(streams, preference=preference)
    if selection is None:
        return None

    roles: list[Transfer] = [
        Transfer(
            url=stream.url or "",
            role="video" if stream.has_video else ("audio" if stream.has_audio else "file"),
            filesize=stream.filesize,
        )
        for stream in selection.streams
    ]
    if any(not transfer.url for transfer in roles):
        return None

    title = _text(info.get("title")) or DEFAULT_FILENAME
    kind: MediaKind = "document" if not selection.playable else kind_for(selection.container)
    if selection.playable and selection.audio is None and not selection.video.has_video:
        kind = "audio"

    return ResolvedFile(
        transfers=tuple(roles),
        filename=f"{title}.{selection.container}",
        media_kind=kind,
        filesize=selection.approx_bytes,
        source_url=_text(info.get("webpage_url")) or source_url,
        playable=selection.playable,
        note=selection.reason,
    )


def _file_from_ytdlp_metadata(
    metadata: Mapping[str, Any],
    *,
    preference: Preference,
    source_url: str | None,
) -> ResolvedFile | None:
    """A ``ytdl:`` record, resolved through the info dict gallery-dl embedded."""
    if not metadata.get("formats"):
        return None
    return _file_from_ytdlp_info(metadata, preference, source_url)


# ── invocation ──────────────────────────────────────────────────────────


async def _run_gallerydl(
    url: str,
    *,
    timeout: float = DEFAULT_RESOLVE_TIMEOUT,
    executable: str = GALLERYDL_EXECUTABLE,
    cookies_file: Path | None = None,
    extractor_options: Mapping[str, str] | None = None,
) -> str:
    """Run gallery-dl and return stdout. Raises :class:`ResolveError` only for *process* failures.

    A non-zero exit is not itself a failure: gallery-dl exits 64 for an unsupported URL, which is
    worth reporting, but exit 0 with an error record is equally a failure. Both are left to
    :func:`parse_gallerydl`, which reads what actually happened.
    """
    process = await asyncio.create_subprocess_exec(
        *gallerydl_command(
            url,
            executable=executable,
            cookies_file=cookies_file,
            extractor_options=extractor_options,
        ),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
    except TimeoutError:
        process.kill()
        await process.wait()
        raise ResolveError(f"the extractor did not answer within {timeout:.0f}s") from None

    if process.returncode not in (0, None) and not stdout.strip():
        detail = stderr.decode("utf-8", "replace").strip().splitlines()
        raise ResolveError(detail[-1] if detail else f"the extractor exited {process.returncode}")
    return stdout.decode("utf-8", "replace")


async def _run_ytdlp(
    url: str,
    *,
    timeout: float = DEFAULT_RESOLVE_TIMEOUT,
    cookies_file: Path | None = None,
) -> Mapping[str, Any]:
    """Resolve through the yt-dlp library, off the event loop.

    ``extract_info`` is blocking and does its own network I/O, so it runs in a worker thread; the
    timeout applies to the whole call. No format selector is passed — the *policy* belongs to
    :mod:`downloader.selection`, and duplicating it as a format string would give two places to
    change and two ways to disagree.
    """
    import yt_dlp

    def extract() -> Mapping[str, Any]:
        options: dict[str, Any] = {"quiet": True, "no_warnings": True, "skip_download": True}
        if cookies_file is not None:
            options["cookiefile"] = str(cookies_file)
        with yt_dlp.YoutubeDL(options) as downloader:
            info = downloader.extract_info(url, download=False)
        if not isinstance(info, Mapping):
            raise ResolveError("the video resolver returned an unexpected shape")
        return info

    try:
        return await asyncio.wait_for(asyncio.to_thread(extract), timeout=timeout)
    except TimeoutError:
        raise ResolveError(f"the video resolver did not answer within {timeout:.0f}s") from None
    except ResolveError:
        raise
    except Exception as error:  # noqa: BLE001 - the engine's message is what the user needs
        raise ResolveError(str(error).strip() or "the video resolver failed") from error


async def resolve_gallerydl(
    url: str,
    *,
    preference: Preference = "compatible",
    timeout: float = DEFAULT_RESOLVE_TIMEOUT,
    cookies_file: Path | None = None,
    extractor_options: Mapping[str, str] | None = None,
    run: GallerydlRunner | None = None,
) -> Resolution:
    """Resolve through gallery-dl alone.

    Separate from :func:`resolve` because the chain (D6) needs to try engines in a chosen order,
    where the router can only pick one from the host. An empty resolution means "this engine found
    no media"; a :class:`ResolveError` means the attempt failed — the distinction E4 and E2 need.
    """
    runner = run or _run_gallerydl
    stdout = await runner(
        url,
        timeout=timeout,
        cookies_file=cookies_file,
        extractor_options=extractor_options,
    )
    parsed = parse_gallerydl(stdout, preference=preference, source_url=url)
    if not parsed.files:
        raise ResolveError(parsed.reason or "nothing downloadable at that link", empty=parsed.empty)
    log.debug("resolved %s via gallery-dl: %d file(s)", url, len(parsed.files))
    return Resolution(engine="gallerydl", source_url=url, files=parsed.files)


async def resolve_ytdlp(
    url: str,
    *,
    preference: Preference = "compatible",
    timeout: float = DEFAULT_RESOLVE_TIMEOUT,
    cookies_file: Path | None = None,
    run: YtdlpRunner | None = None,
) -> Resolution:
    """Resolve through the yt-dlp library alone (D5's native route for video hosts)."""
    runner = run or _run_ytdlp
    info = await runner(url, timeout=timeout, cookies_file=cookies_file)
    files = files_from_ytdlp(info, preference=preference, source_url=url)
    if not files:
        raise ResolveError("the video resolver found nothing downloadable", empty=True)
    log.debug("resolved %s via yt-dlp: %d file(s)", url, len(files))
    return Resolution(engine="ytdlp", source_url=url, files=tuple(files))


async def resolve(
    url: str,
    *,
    preference: Preference = "compatible",
    timeout: float = DEFAULT_RESOLVE_TIMEOUT,
    run_gallerydl: GallerydlRunner | None = None,
    run_ytdlp: YtdlpRunner | None = None,
) -> Resolution:
    """Resolve one link with whichever engine its host calls for.

    The by-host router, kept for callers that have no chain to run; :mod:`downloader.registry`
    builds chains out of :func:`resolve_gallerydl` and :func:`resolve_ytdlp` instead.

    Raises :class:`ResolveError` when nothing fetchable came back, carrying a reason worth showing.
    """
    if engine_for(url) == "ytdlp":
        return await resolve_ytdlp(url, preference=preference, timeout=timeout, run=run_ytdlp)
    return await resolve_gallerydl(url, preference=preference, timeout=timeout, run=run_gallerydl)
