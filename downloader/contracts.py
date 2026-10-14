"""What resolving a link produces: files to deliver, and the transfers that produce them.

The unit of delivery and the unit of transfer are deliberately **not** the same thing. Every
YouTube format is a merge now — measured: 0 of 53 formats carry both video and audio — so one file
the user receives can be two HTTP transfers plus an ffmpeg pass. Modelling them separately is what
lets the fetcher report progress per stream, and lets the size gate decide on the *result* rather
than on whichever stream happened to be counted first.

Keeping this contract free of engine details is what makes the rest of the pipeline testable: the
resolvers in :mod:`downloader.probe` translate gallery-dl's typed records and yt-dlp's info dict
into these types, and nothing downstream has to know which engine ran.
"""

from __future__ import annotations

import itertools
import re
from collections.abc import Collection, Mapping
from dataclasses import dataclass, field
from typing import Literal

MediaKind = Literal["photo", "video", "audio", "document"]
#: ``hls`` is a *manifest*, not media: the fetcher consumes it with ffmpeg, which is why it is a
#: role of its own rather than a ``video`` transfer. Treating an ``.m3u8`` as a plain download (the
#: obvious mistake) writes a text playlist to disk and names it ``.mp4``.
TransferRole = Literal["video", "audio", "file", "hls"]

#: What the delivery layer can send as media rather than as a plain file.
PHOTO_EXTENSIONS = frozenset({"jpg", "jpeg", "png", "webp", "gif", "bmp", "heic"})
VIDEO_EXTENSIONS = frozenset({"mp4", "mkv", "webm", "mov", "avi", "m4v", "ts"})
AUDIO_EXTENSIONS = frozenset({"mp3", "m4a", "aac", "opus", "ogg", "flac", "wav", "wma"})

#: Long enough for a title to stay readable in a chat, short enough not to fight filesystem limits
#: once the extension is appended.
MAX_FILENAME_LENGTH = 120
FALLBACK_FILENAME = "spideybot-download"

_UNSAFE_FILENAME = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_COLLAPSED_SPACES = re.compile(r"\s+")


class ResolveError(RuntimeError):
    """A link could not be resolved into anything fetchable.

    Carries a reason fit to show a user: the pipeline reports it rather than a traceback
    (FR-9, E2).

    ``empty`` separates the two reasons a link can yield nothing, because they are two different
    messages to the user: a share that holds no media is E4 (*"No actual files found in this
    share"*), while an extractor that failed is E2 (report its reason). Both mean "no files from
    this attempt", so both move on to the next resolver in the chain — but only the failure is
    worth repeating back, and only when every resolver failed.
    """

    def __init__(self, message: str, *, empty: bool = False) -> None:
        super().__init__(message)
        self.empty = empty


def kind_for(extension: str | None) -> MediaKind:
    """Classify by extension. Unknown or absent means a plain document, which is always safe."""
    if not extension:
        return "document"
    lowered = extension.lower().lstrip(".")
    if lowered in PHOTO_EXTENSIONS:
        return "photo"
    if lowered in VIDEO_EXTENSIONS:
        return "video"
    if lowered in AUDIO_EXTENSIONS:
        return "audio"
    return "document"


def safe_filename(name: str, *, fallback: str = FALLBACK_FILENAME) -> str:
    """A filename that cannot escape its directory or upset a filesystem.

    Necessary because titles come from the internet: a title containing a path separator would
    otherwise write outside the staging directory, and one containing a NUL or a newline would
    break the tools that consume it.
    """
    cleaned = _UNSAFE_FILENAME.sub("_", name)
    cleaned = _COLLAPSED_SPACES.sub(" ", cleaned).strip(" .")
    if not cleaned:
        return fallback
    return cleaned[:MAX_FILENAME_LENGTH].rstrip(" .") or fallback


def unique_name(name: str, taken: Collection[str]) -> str:
    """``name``, or the first numbered variant of it that is not already in ``taken``.

    A share can hold two files with the same title as easily as it can hold one, and the second
    would otherwise overwrite the first: the user would be told two files were downloaded and
    receive one. Numbering keeps the extension — delivery decides the media kind from it — and
    counts from 2 so the name reads as "another one of these" rather than as a version.
    """
    if name not in taken:
        return name

    stem, dot, extension = name.rpartition(".")
    if not dot:
        stem, extension = name, ""

    for counter in itertools.count(2):
        suffix = f" ({counter})"
        room = MAX_FILENAME_LENGTH - len(suffix) - len(extension) - (1 if dot else 0)
        candidate = f"{stem[:room]}{suffix}{dot}{extension}"
        if candidate not in taken:
            return candidate
    raise AssertionError("unreachable")  # pragma: no cover - itertools.count is unbounded


@dataclass(frozen=True)
class Transfer:
    """One HTTP transfer. A merged video has two of these."""

    url: str
    role: TransferRole = "file"
    filesize: int | None = None
    #: Headers this transfer needs beyond the fetcher's browser defaults and its per-host map — a
    #: cookie, or a `Referer` that has to match the page the link came from. Only the resolver knows
    #: what the host demanded *during resolution*, so it is the only party that can supply this.
    #: Excluded from equality because a mapping is unhashable and a `Transfer` is compared in tests.
    headers: Mapping[str, str] | None = field(default=None, compare=False)


@dataclass(frozen=True)
class ResolvedFile:
    """One file the user will receive, and what it takes to produce it."""

    transfers: tuple[Transfer, ...]
    filename: str
    media_kind: MediaKind = "document"
    filesize: int | None = None
    source_url: str | None = None
    #: Whether Telegram will play this inline. When false the delivery layer sends it as a
    #: document, with ``note`` explaining why (D28).
    playable: bool = True
    note: str | None = None

    @property
    def needs_merge(self) -> bool:
        return len(self.transfers) > 1

    @property
    def primary(self) -> Transfer:
        return self.transfers[0]

    @property
    def expected_bytes(self) -> int | None:
        """The size the size gate should judge, or ``None`` when any stream's size is unknown.

        ``None`` is not zero, and zero is not ``None``: a reported zero is a real measurement (an
        empty file, which E6 reports as a failure), while an unknown size must never be read as one
        — the size gate would then wave through a download it cannot measure (E3).
        """
        if self.filesize is not None:
            return self.filesize
        sizes = [transfer.filesize for transfer in self.transfers]
        if not sizes or any(size is None for size in sizes):
            return None
        return sum(size for size in sizes if size is not None)


@dataclass(frozen=True)
class Resolution:
    """Everything a resolver found for one link."""

    engine: str
    source_url: str
    files: tuple[ResolvedFile, ...] = ()

    def __bool__(self) -> bool:
        return bool(self.files)

    @property
    def total_bytes(self) -> int | None:
        """The whole job's size, or ``None`` if any file's size is unknown.

        Unknown poisons the total on purpose: a partial sum would look like a precise number and
        could pass a size gate the real download would fail (AC-3).
        """
        sizes = [item.expected_bytes for item in self.files]
        if not sizes or any(size is None for size in sizes):
            return None
        return sum(size for size in sizes if size is not None)
