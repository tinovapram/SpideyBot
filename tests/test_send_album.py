"""Album delivery: photos/videos are grouped, everything else goes alone."""

from __future__ import annotations

from typing import Any

import pytest
from telethon import types

from utils.telethon import ALBUM_LIMIT, is_album_media, send_album


def _handle() -> Any:
    """Stand-in for an uploaded file handle."""
    return object()


def _photo() -> types.InputMediaUploadedPhoto:
    return types.InputMediaUploadedPhoto(file=_handle())


def _video() -> types.InputMediaUploadedDocument:
    return types.InputMediaUploadedDocument(
        file=_handle(),
        mime_type="video/mp4",
        attributes=[types.DocumentAttributeVideo(duration=1, w=1, h=1)],
    )


def _document() -> types.InputMediaUploadedDocument:
    return types.InputMediaUploadedDocument(
        file=_handle(),
        mime_type="application/zip",
        attributes=[types.DocumentAttributeFilename(file_name="archive.zip")],
    )


def _animation() -> types.InputMediaUploadedDocument:
    return types.InputMediaUploadedDocument(
        file=_handle(),
        mime_type="video/mp4",
        attributes=[
            types.DocumentAttributeAnimated(),
            types.DocumentAttributeVideo(duration=1, w=1, h=1),
        ],
    )


class FakeClient:
    """Records each ``send_file`` call: (chat_id, payload)."""

    def __init__(self) -> None:
        self.calls: list[tuple[Any, Any]] = []

    async def send_file(self, chat_id: Any, media: Any, **_kwargs: Any) -> None:
        self.calls.append((chat_id, media))


def test_album_media_detection() -> None:
    assert is_album_media(_photo()) is True
    assert is_album_media(_video()) is True
    assert is_album_media(_document()) is False
    assert is_album_media(_animation()) is False


async def test_photos_and_videos_are_grouped_and_documents_sent_alone() -> None:
    client = FakeClient()
    media = [_photo(), _photo(), _document(), _video()]
    captions = ["a", "b", "c", "d"]

    sent = await send_album(client, 1, media, captions, reply_to=5)

    assert sent == 4
    # album(two photos) -> document alone -> album(one video)
    assert len(client.calls) == 3
    assert client.calls[0][1] == [media[0], media[1]]
    assert client.calls[1][1] is media[2]
    assert client.calls[2][1] == [media[3]]


async def test_documents_are_never_batched() -> None:
    client = FakeClient()
    media = [_document(), _document(), _document()]

    sent = await send_album(client, 1, media, ["", "", ""])

    assert sent == 3
    assert [call[1] for call in client.calls] == media
    assert all(not isinstance(call[1], list) for call in client.calls)


async def test_albums_are_chunked_to_the_telegram_limit() -> None:
    client = FakeClient()
    media = [_photo() for _ in range(ALBUM_LIMIT + 2)]

    sent = await send_album(client, 1, media, [""] * len(media))

    assert sent == ALBUM_LIMIT + 2
    assert [len(call[1]) for call in client.calls] == [ALBUM_LIMIT, 2]


async def test_missing_captions_do_not_drop_media() -> None:
    client = FakeClient()

    sent = await send_album(client, 1, [_photo(), _video()], [])

    assert sent == 2
    assert len(client.calls) == 1


async def test_album_failure_falls_back_to_individual_sends(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = FakeClient()
    original_send = client.send_file
    albums_attempted = {"count": 0}
    photos = [_photo(), _photo()]

    async def flaky_send_file(chat_id: Any, media: Any, **kwargs: Any) -> None:
        if isinstance(media, list) and albums_attempted["count"] == 0:
            albums_attempted["count"] += 1
            raise RuntimeError("MEDIA_INVALID")
        await original_send(chat_id, media, **kwargs)

    monkeypatch.setattr(client, "send_file", flaky_send_file)

    sent = await send_album(client, 1, photos, ["", ""])

    assert sent == 2
    assert [call[1] for call in client.calls] == photos
