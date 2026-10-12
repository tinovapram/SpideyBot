"""Fetching media out of Telegram messages with the user's own session (FR-4, AC-7).

This is the one source in the project that is not HTTP. Where :mod:`downloader.flow` resolves a link
into URLs and then fetches them, a Telegram message *is* the media: the file lives on Telegram's
servers, and only a client signed in as the user can ask for it. So this module produces the same
thing the pipeline produces — :class:`~downloader.fetcher.FetchedFile` objects on disk plus a
failure list — and the job handler treats both identically from there on (gate, ledger, albums,
captions).

Three details are the reason this is not a ten-line function:

* **An album is one message plus its neighbours.** A grouped post is a set of messages sharing
  ``media_group_id``, and the link a user copies points at exactly one of them. The group is found
  by fetching a window around the linked message — V5's approach, kept because a link cannot express
  "and the ten beside it" — and then filtered by that group id.
* **Every file is identified by its message.** ``resolved_url`` is set to a ``tg:`` identity rather
  than left empty, because the job ledger's whole job is to stop a resumed job re-uploading what it
  already sent (§8.2.3), and for Telegram the message *is* the identity of the file.
* **Captions are carried, not reconstructed.** FR-4.4 asks for them preserved, and the only place
  the text exists is the message it was posted on — so each file carries its own caption, and one is
  generated only when the post had none.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app import catalog
from downloader import gate
from downloader.contracts import MediaKind
from downloader.fetcher import FetchedFile
from telegram import links

log = logging.getLogger(__name__)

#: How far around a linked message to look for the rest of its album. Telegram sends the group as
#: consecutive messages, so the window only has to be larger than the largest album (10).
ALBUM_WINDOW = 10

#: How many messages a folder scan looks through per chat before deciding there is nothing recent.
FOLDER_SCAN = 10

#: Progress through a Telegram download, as ``(bytes_done, bytes_total)`` for the whole fetch.
Progress = Callable[[int, int], Awaitable[None] | None]

#: Whether the job has been cancelled (FR-11.6).
StopCheck = Callable[[], bool]

#: The chat a saved file goes to: Telegram's own "me" (Saved Messages). Both FR-5 and FR-6 file what
#: they save there, and the string is the API's, not ours.
SELF = "me"


@dataclass(frozen=True)
class Fetched:
    """What a Telegram fetch produced, in the vocabulary the job handler already speaks."""

    files: tuple[FetchedFile, ...] = ()
    #: ``(name, reason)`` per file that failed, converted to ``catalog.Failure`` by the caller.
    failures: tuple[tuple[str, str], ...] = ()
    #: FR-4.5's counts: how many messages were *looked at*, not how many had media.
    scanned: int = 0
    #: The source chat's title, for the message the user reads.
    chat: str = ""
    #: A user-facing reason when nothing could be fetched at all (E2's shape, one sentence).
    error: str | None = None
    #: True when the message(s) were *reachable but empty* — E4's category, not a failure. A retry
    #: cannot change this verdict, which is why it is separated from ``error``.
    empty: bool = False
    #: True when the fetch stopped early because the user cancelled (FR-11.6).
    cancelled: bool = False
    #: The size resolution reported before anything was transferred, so the tier's limit can be
    #: judged first (AC-3) — the same rule the HTTP path follows.
    total_bytes: int = 0
    #: AC-3's verdict when the link was too large. Returned rather than rendered, so the refusal
    #: sentence and its two numbers come from the same place as the HTTP path's.
    refused: gate.SizeCheck | None = None


async def fetch(
    client: Any,
    target: links.TgTarget,
    staging: Path,
    *,
    limit_mb: int = 0,
    should_stop: StopCheck | None = None,
    on_progress: Progress | None = None,
) -> Fetched:
    """Download what a link points at. Never raises for a source-side problem: it reports.

    A refusal here is a *sentence*, because that is what gets shown to the user: the caller decides
    how to render it. ``limit_mb`` is the tier's per-link cap and is judged on Telegram's own size
    metadata before anything is requested (AC-3); ``0`` means unlimited, as it does everywhere else.
    """
    staging.mkdir(parents=True, exist_ok=True)
    stop = should_stop or (lambda: False)

    if isinstance(target, links.FolderLink):
        return await _folder(
            client, target, staging, limit_mb=limit_mb, stop=stop, on_progress=on_progress
        )
    if isinstance(target, links.RangeLink):
        return await _range_(
            client, target, staging, limit_mb=limit_mb, stop=stop, on_progress=on_progress
        )
    return await _single(
        client, target, staging, limit_mb=limit_mb, stop=stop, on_progress=on_progress
    )


# ── one message, and its album if it has one ────────────────────────────


async def _single(
    client: Any,
    link: links.MessageLink,
    staging: Path,
    *,
    limit_mb: int,
    stop: StopCheck,
    on_progress: Progress | None,
) -> Fetched:
    chat, title, failure = await _open_chat(client, link)
    if failure is not None:
        return failure

    message = await _message(client, chat, link.message_id)
    if message is None:
        return Fetched(
            error=catalog.DT_MESSAGE_MISSING.format(id=link.message_id, chat=_name(link)),
            chat=title,
        )

    group = _group_id(message)
    if group is None:
        candidates = [message]
    else:
        window = await _window(client, chat, link.message_id)
        candidates = [m for m in window if _group_id(m) == group]

    return await _download(
        client,
        candidates,
        staging,
        chat_title=title,
        scanned=len(candidates),
        chat_hint=_name(link),
        limit_mb=limit_mb,
        stop=stop,
        on_progress=on_progress,
    )


# ── a range of messages ─────────────────────────────────────────────────


async def _range_(
    client: Any,
    target: links.RangeLink,
    staging: Path,
    *,
    limit_mb: int,
    stop: StopCheck,
    on_progress: Progress | None,
) -> Fetched:
    link = target.start
    chat, title, failure = await _open_chat(client, link)
    if failure is not None:
        return failure

    fetched = await _messages(client, chat, target.ids())
    # A range is counted in messages *scanned*, including the ones with no media (FR-4.5), so an
    # empty range and a range of text posts are different answers.
    scanned = len([m for m in fetched if m is not None])
    if not any(_has_media(m) for m in fetched):
        return Fetched(
            scanned=scanned,
            chat=title,
            empty=True,
            error=catalog.DT_RANGE_EMPTY.format(
                start=target.start.message_id, end=target.end.message_id, chat=_name(link)
            ),
        )

    return await _download(
        client,
        [m for m in fetched if m is not None],
        staging,
        chat_title=title,
        scanned=scanned,
        chat_hint=_name(link),
        limit_mb=limit_mb,
        stop=stop,
        on_progress=on_progress,
    )


# ── a shared folder ─────────────────────────────────────────────────────


async def _folder(
    client: Any,
    target: links.FolderLink,
    staging: Path,
    *,
    limit_mb: int,
    stop: StopCheck,
    on_progress: Progress | None,
) -> Fetched:
    """The most recent media in each chat of a shared folder.

    A folder link names no content at all — it is a filter over chats — so "download the folder"
    has to be bounded somewhere, and the bound chosen here is one message per chat: each chat is
    scanned from its newest message until something with media is found (:data:`FOLDER_SCAN` at
    most), and that media is downloaded. Downloading *everything* in a folder is unbounded and would
    quietly spend a user's whole allowance on a link they clicked once, which no tier limit can
    catch before the fact.
    """
    try:
        folders = await client.get_folders()
    except Exception as failed:
        log.warning("telegram: reading folders failed: %s", failed)
        return Fetched(
            error=catalog.DT_CHAT_UNREACHABLE.format(chat=target.slug, reason=str(failed))
        )

    folder = next(
        (
            candidate
            for candidate in folders
            if str(getattr(candidate, "slug", "") or "").lower() == target.slug.lower()
            or str(getattr(candidate, "id", "") or "") == target.slug
        ),
        None,
    )
    if folder is None:
        return Fetched(error=catalog.DT_FOLDER_EMPTY, empty=True)

    chats: list[Any] = []
    for attribute in ("included_chats", "pinned_chats"):
        chats.extend(getattr(folder, attribute, None) or [])

    picked: list[Any] = []
    scanned = 0
    for chat in chats:
        if stop():
            break
        history = await _history(client, chat, FOLDER_SCAN)
        scanned += len(history)
        for message in history:
            if _has_media(message):
                picked.append(message)
                break

    if not picked:
        return Fetched(scanned=scanned, error=catalog.DT_FOLDER_EMPTY, empty=True)

    return await _download(
        client,
        picked,
        staging,
        chat_title=str(getattr(folder, "title", "") or target.slug),
        scanned=scanned,
        chat_hint=target.slug,
        limit_mb=limit_mb,
        stop=stop,
        on_progress=on_progress,
    )


# ── the download itself, shared by all three shapes ─────────────────────


async def _download(
    client: Any,
    messages: Sequence[Any],
    staging: Path,
    *,
    chat_title: str,
    scanned: int,
    chat_hint: str,
    limit_mb: int,
    stop: StopCheck,
    on_progress: Progress | None,
) -> Fetched:
    media = [message for message in messages if _has_media(message)]
    if not media:
        return Fetched(scanned=scanned, chat=chat_title, error=catalog.DT_NO_MEDIA, empty=True)

    # The total is AC-3's shape: known from the messages before a byte moves, so the tier's limit
    # can refuse the whole link rather than stopping it halfway. Telegram reports real sizes for
    # everything it stores, so unlike an extractor's guess this number is reliable.
    total = sum(_size(message) for message in media)
    verdict = gate.check_size(total, limit_mb=limit_mb)
    if not verdict.allowed:
        return Fetched(
            scanned=scanned, chat=chat_title, total_bytes=total, refused=verdict
        )

    files: list[FetchedFile] = []
    failures: list[tuple[str, str]] = []
    for message in media:
        if stop():
            return Fetched(
                files=tuple(files),
                failures=tuple(failures),
                scanned=scanned,
                chat=chat_title,
                cancelled=True,
                total_bytes=total,
            )
        try:
            # ``download_media`` with a directory writes Telegram's own filename there, which is
            # what keeps a photo's name, an audio track's title and a document's name intact.
            path = await client.download_media(message, file_name=str(staging))
        except Exception as failed:  # noqa: BLE001 - one file's failure is not the job's
            name = _filename(message)
            log.warning("telegram: downloading message %s failed: %s", _id(message), failed)
            failures.append((name, str(failed) or type(failed).__name__))
            continue
        if not path:
            failures.append((_filename(message), catalog.EMPTY_FILE_REASON))
            continue

        files.append(
            FetchedFile(
                path=Path(path),
                media_kind=_kind(message),
                bytes_written=_on_disk(Path(path), _size(message)),
                source_url=_message_link(chat_hint, _id(message)),
                resolved_url=f"tg:{chat_hint}:{_id(message)}",
                caption=_caption(message, chat_title),
            )
        )
        if on_progress is not None:
            await _progress(on_progress, sum(file.bytes_written for file in files), total)
    if not files and not failures:
        return Fetched(scanned=scanned, chat=chat_title, error=catalog.DT_NO_MEDIA, empty=True)

    return Fetched(
        files=tuple(files),
        failures=tuple(failures),
        scanned=scanned,
        chat=chat_title,
        total_bytes=total,
    )


# ── client calls, each one tolerant of its own failure ──────────────────


async def _open_chat(
    client: Any, link: links.MessageLink
) -> tuple[Any, str, Fetched | None]:
    """Resolve the chat, or the refusal to report instead."""
    try:
        chat = await client.get_chat(link.chat_id)
    except Exception as failed:
        return None, _name(link), Fetched(
            error=catalog.DT_CHAT_UNREACHABLE.format(chat=_name(link), reason=str(failed)),
            chat=_name(link),
        )
    title = str(getattr(chat, "title", None) or getattr(chat, "username", "") or _name(link))
    return chat, title, None


async def _message(client: Any, chat: Any, message_id: int) -> Any:
    try:
        return await client.get_messages(chat.id, message_ids=message_id)
    except Exception as failed:
        log.warning("telegram: fetching message %s failed: %s", message_id, failed)
        return None


async def _window(client: Any, chat: Any, message_id: int) -> list[Any]:
    """The messages around a link, newest last, for finding the rest of an album."""
    ids = list(range(max(1, message_id - ALBUM_WINDOW), message_id + ALBUM_WINDOW + 1))
    return [message for message in await _messages(client, chat, ids) if message is not None]


async def _messages(client: Any, chat: Any, ids: Sequence[int]) -> list[Any]:
    """Fetch a list of ids, as a list (the library returns one message for one id)."""
    try:
        result = await client.get_messages(chat.id, message_ids=list(ids))
    except Exception as failed:
        log.warning("telegram: fetching %d messages failed: %s", len(ids), failed)
        return []
    if result is None:
        return []
    return list(result) if isinstance(result, list) else [result]


async def send_to_self(
    client: Any,
    message: Any,
    *,
    caption: str,
    scratch: Path,
) -> str:
    """Keep one message's media in the user's own Saved Messages (FR-5.5, FR-6.3).

    ``chat_id="me"`` is Telegram's self-chat, which is where both features file what they save: the
    command that triggered them may have been typed in someone else's chat, and replying there — as
    the user or as the bot — would be a surprise with consequences the user cannot undo. Downloading
    first and re-sending keeps the copy *ours* (a forwarded message shows the original sender, which
    for a saved file is exactly what the user did not ask for) and gives the caption a place to go.

    Returns the path it wrote, so a caller can clean up or report a size.
    """
    scratch.mkdir(parents=True, exist_ok=True)
    path = await client.download_media(message, file_name=str(scratch))
    if not path:
        raise RuntimeError("Telegram returned no file for that message")

    kind = _kind(message)
    sender = {
        "photo": client.send_photo,
        "video": client.send_video,
        "audio": client.send_audio,
        "document": client.send_document,
    }[kind]
    await sender(chat_id=SELF, **{kind: path}, caption=caption)
    return str(path)


async def _history(client: Any, chat: Any, limit: int) -> list[Any]:
    """The newest messages of a chat, newest first, for the folder scan."""
    identifier = getattr(chat, "id", None) or getattr(chat, "username", None)
    if identifier is None:
        return []
    try:
        result = await client.get_messages(identifier, limit=limit)
    except Exception as failed:
        log.warning("telegram: reading the history of %s failed: %s", identifier, failed)
        return []
    return list(result) if isinstance(result, list) else [result]


async def _progress(hook: Progress | None, done: int, total: int) -> None:
    """Report progress to a hook that may be sync or async, and may be absent (as flow._notify)."""
    if hook is None:
        return
    result = hook(done, total)
    if isinstance(result, Awaitable):
        await result


# ── what a message says about itself ────────────────────────────────────


def _has_media(message: Any) -> bool:
    return getattr(message, "media", None) is not None


def _group_id(message: Any) -> Any:
    """The album this message belongs to, if any. Kurigram spells it ``media_group_id``."""
    return getattr(message, "media_group_id", None) or getattr(message, "grouped_id", None)


def _id(message: Any) -> int:
    return int(getattr(message, "id", 0) or 0)


def _size(message: Any) -> int:
    """The file's size as Telegram reports it. Zero when it says nothing, never a guess."""
    for attribute in ("document", "video", "audio", "voice", "animation"):
        document = getattr(message, attribute, None)
        size = getattr(document, "file_size", None)
        if size:
            return int(size)
    photo = getattr(message, "photo", None)
    size = getattr(photo, "file_size", None)
    if size:
        return int(size)
    return 0


def _filename(message: Any) -> str:
    """A name for the failure list, before or after the download decided one."""
    for attribute in ("document", "video", "audio", "voice", "animation"):
        document = getattr(message, attribute, None)
        name = getattr(document, "file_name", None)
        if name:
            return str(name)
    return f"message {_id(message)}"


def _camel(enum: Any) -> str:
    """``MediaKind.VIDEO`` → ``"video"``."""
    return str(getattr(enum, "name", enum)).lower()


def _kind(message: Any) -> MediaKind:
    """FR-8.1's kind, from what the message carries.

    A secret/self-destruct video (TimerMedia) arrives as a *document* flagged with a short TTL in
    some clients, and sending it as a document is the honest answer: it has no streaming metadata to
    re-send as a playable video.
    """
    if getattr(message, "photo", None) is not None:
        return "photo"
    if getattr(message, "video", None) is not None:
        return "video"
    if getattr(message, "video_note", None) is not None:
        return "video"
    if getattr(message, "audio", None) is not None or getattr(message, "voice", None) is not None:
        return "audio"
    return "document"


def _caption(message: Any, chat_title: str) -> str:
    """FR-4.4: the post's own caption when it had one, a summary when it did not."""
    text = getattr(message, "caption", None) or getattr(message, "text", None)
    if text:
        return str(text)
    return catalog.DT_CAPTION_FALLBACK.format(chat=chat_title, id=_id(message))


def _message_link(chat_hint: str, message_id: int) -> str | None:
    """A link back to the source message, when the chat is addressable by name."""
    if not chat_hint or chat_hint.isdigit():
        # A private chat is addressed as `c/<internal>`, and the internal id is not the hint we
        # hold; a wrong link is worse than none, so none is what gets returned.
        return None
    return f"https://t.me/{chat_hint}/{message_id}"


def _name(link: links.MessageLink) -> str:
    """How the copy names a chat: the username, or the id for a private one."""
    return link.chat


def _on_disk(path: Path, reported: int) -> int:
    """What the file weighs, falling back to Telegram's number if the path is unreadable."""
    try:
        return path.stat().st_size
    except OSError:  # pragma: no cover - the file was just written
        return reported
