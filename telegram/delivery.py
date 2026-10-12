"""Handing files to Telegram (FR-8, D24).

Three rules shape this module, and all three are the spec's:

* **Media keeps its kind** (FR-8.1): a photo is sent as a photo, a video as a video, anything else
  as a document. Getting this wrong is not cosmetic — a video sent as a document has no thumbnail
  and does not play in place, which is most of the value of a downloader bot.
* **Independent files become albums of up to 10, and the remainder is flushed** (FR-8.2). Files that
  *are* a file and its own parts — the two halves of a merge — never appear here at all: merging
  happens in the fetcher, so by the time anything reaches this module one entry is one deliverable
  (D30).
* **A file is delivered once** (FR-8.5). This module does not enforce that; it makes enforcing it
  possible, by reporting exactly which message ids carried which file, so the job ledger can record
  the fact before the next file is attempted.

It deliberately knows nothing about the database, the queue or the pipeline's outcome types: it
takes finished files and a chat, and it sends. That is what lets the delivery rules be tested
against a recording fake with no client, no network and no job.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from app import catalog
from downloader.fetcher import FetchedFile

log = logging.getLogger(__name__)

#: FR-8.2's album size. A Telegram limit, not a preference: the API refuses more than ten.
ALBUM_SIZE = 10


@dataclass(frozen=True)
class Delivered:
    """One file that reached Telegram, and the message(s) carrying it."""

    file: FetchedFile
    message_ids: tuple[int, ...]
    #: True when it travelled inside an album, which matters for caption and edit decisions.
    album: bool = False


class Sender(Protocol):
    """The client methods delivery needs, so the real Kurigram client and a recorder both fit."""

    async def send_photo(self, **kwargs: Any) -> Any: ...

    async def send_video(self, **kwargs: Any) -> Any: ...

    async def send_audio(self, **kwargs: Any) -> Any: ...

    async def send_document(self, **kwargs: Any) -> Any: ...

    async def send_media_group(self, **kwargs: Any) -> Sequence[Any]: ...


#: How a file's caption is built. The caller owns the wording (D23) and the source link (FR-8.3).
CaptionOf = Callable[[FetchedFile], str | None]

#: Progress through the upload, as ``(files_uploaded, files_total)``. FR-7.5 asks the status message
#: for per-file upload context (``Uploading (file 7/25)``), and this module is the only place that
#: knows how far the upload got — so it reports, and the status message decides the wording.
UploadHook = Callable[[int, int], Awaitable[None] | None]

#: How the sender is fetched for a chat. A callable rather than a value because the client for a
#: user is looked up per job — a bot client, or that user's own session (D10, P5).
SenderFor = Callable[[int], Sender | Awaitable[Sender]]


async def resolve_sender(sender_for: SenderFor, chat_id: int) -> Sender:
    """The sender to use for a chat, awaiting the lookup when it is asynchronous."""
    sender = sender_for(chat_id)
    if isinstance(sender, Awaitable):
        return await sender
    return sender


def _media_for(file: FetchedFile, caption: str | None) -> Any:
    """The ``InputMedia`` that preserves this file's kind (FR-8.1)."""
    from pyrogram.types import (
        InputMediaAudio,
        InputMediaDocument,
        InputMediaPhoto,
        InputMediaVideo,
    )

    path = str(file.path)
    match file.media_kind:
        case "photo":
            return InputMediaPhoto(media=path, caption=caption)
        case "video":
            return InputMediaVideo(media=path, caption=caption)
        case "audio":
            return InputMediaAudio(media=path, caption=caption)
        case _:
            return InputMediaDocument(media=path, caption=caption)


def _single_send(sender: Sender, kind: str, kwargs: dict[str, Any]) -> Awaitable[Any]:
    match kind:
        case "photo":
            return sender.send_photo(**kwargs)
        case "video":
            return sender.send_video(**kwargs)
        case "audio":
            return sender.send_audio(**kwargs)
        case _:
            return sender.send_document(**kwargs)


async def deliver(
    files: Sequence[FetchedFile],
    *,
    chat_id: int,
    sender_for: SenderFor,
    reply_to_message_id: int | None = None,
    caption_of: CaptionOf | None = None,
    album_size: int = ALBUM_SIZE,
    on_upload: UploadHook | None = None,
) -> tuple[Delivered, ...]:
    """Send every file, in order, in albums of at most ``album_size``.

    Returns what was sent, so the caller can ledger it. Raises nothing for an individual failure:
    FR-9.1 says one bad file must not stop the others, and the caller gets the successes back to
    report the rest as failures.

    ``on_upload`` is called after each album with how many files have reached Telegram, so the
    status message can show FR-7.5's per-file context. It counts *delivered* files rather than
    attempted ones, because the number the user is watching should only go up when something
    actually arrived.
    """
    sender = await resolve_sender(sender_for, chat_id)
    delivered: list[Delivered] = []
    total = len(files)
    index = 0

    while index < len(files):
        batch = list(files[index : index + album_size])
        if len(batch) == 1:
            delivered.extend(
                await _send_one(
                    sender,
                    batch[0],
                    chat_id,
                    reply_to_message_id=reply_to_message_id,
                    caption_of=caption_of,
                )
            )
        else:
            delivered.extend(
                await _send_album(
                    sender,
                    batch,
                    chat_id,
                    reply_to_message_id=reply_to_message_id,
                    caption_of=caption_of,
                )
            )
        index += len(batch)
        await _report(on_upload, len(delivered), total)

    log.info("delivered %d file(s) to %s", len(delivered), chat_id)
    return tuple(delivered)


async def _report(hook: UploadHook | None, done: int, total: int) -> None:
    """Call the upload hook, tolerating one that is a plain function rather than a coroutine."""
    if hook is None:
        return
    result = hook(done, total)
    if isinstance(result, Awaitable):
        await result


async def _send_one(
    sender: Sender,
    file: FetchedFile,
    chat_id: int,
    *,
    reply_to_message_id: int | None,
    caption_of: CaptionOf | None,
) -> list[Delivered]:
    caption = caption_of(file) if caption_of is not None else None
    kwargs: dict[str, Any] = {"chat_id": chat_id, file.media_kind: file.path}
    if caption:
        kwargs["caption"] = caption
    if reply_to_message_id is not None:
        kwargs["reply_to_message_id"] = reply_to_message_id

    try:
        message = await _single_send(sender, file.media_kind, kwargs)
    except Exception as error:  # noqa: BLE001 - one file must not cost the others (FR-9.1)
        log.warning("could not send %s: %s", file.path.name, error)
        return []
    return [Delivered(file=file, message_ids=_ids(message))]


async def _send_album(
    sender: Sender,
    batch: Sequence[FetchedFile],
    chat_id: int,
    *,
    reply_to_message_id: int | None,
    caption_of: CaptionOf | None,
) -> list[Delivered]:
    media = [
        _media_for(file, caption_of(file) if caption_of is not None else None) for file in batch
    ]
    kwargs: dict[str, Any] = {"chat_id": chat_id, "media": media}
    if reply_to_message_id is not None:
        kwargs["reply_to_message_id"] = reply_to_message_id

    try:
        messages = await sender.send_media_group(**kwargs)
    except Exception as error:  # noqa: BLE001 - as above: the rest of the job still matters
        log.warning("could not send an album of %d file(s): %s", len(batch), error)
        # One at a time, so a single rejected item cannot cost the whole album (FR-9.1).
        single: list[Delivered] = []
        for file in batch:
            single.extend(
                await _send_one(
                    sender,
                    file,
                    chat_id,
                    reply_to_message_id=reply_to_message_id,
                    caption_of=caption_of,
                )
            )
        return single

    ids = [_ids(message)[0] if _ids(message) else None for message in messages]
    return [
        Delivered(file=file, message_ids=((ids[position] or 0),), album=True)
        for position, file in enumerate(batch)
    ]


def _ids(message: Any) -> tuple[int, ...]:
    """The message id, tolerating a fake that reports several or none."""
    if message is None:
        return ()
    identifiers = getattr(message, "message_ids", None)
    if identifiers:
        return tuple(int(value) for value in identifiers)
    single = getattr(message, "id", None)
    return (int(single),) if single is not None else ()


def default_caption(file: FetchedFile) -> str | None:
    """The caption when the caller has nothing better: the source link, when there is one.

    FR-8.3 asks for the source link "where useful", and a file with no link is a file whose origin
    the user already knows — it is the command they just sent.
    """
    if not file.source_url:
        return file.note
    return catalog.SOURCE_CAPTION.format(source=file.source_url)
