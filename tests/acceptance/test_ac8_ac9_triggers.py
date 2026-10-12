"""AC-8 and AC-9: the two things a live session does by itself (FR-5, FR-6).

Both features run on the *user's* client, react to the user's own messages, and file what they
save in Saved Messages. The tests therefore drive the handler functions with a message shaped like
the one Kurigram delivers, and a client that records what it stored — the only faked thing is
Telegram.
"""

from __future__ import annotations

import pytest

from app import catalog, config, paths, prefs, users
from app import db as db_module
from telegram import magic, timer

USER = 880
SENDER = 990


class FakeChat:
    def __init__(self, chat_id: int, *, kind: str = "PRIVATE") -> None:
        self.id = chat_id
        self.type = type("T", (), {"name": kind})()


class FakeUser:
    def __init__(self, user_id: int, username: str | None = None) -> None:
        self.id = user_id
        self.username = username


class FakeDownload:
    """A photo or video holder, as far as ``telegram.media`` reads it."""

    def __init__(self, *, name: str | None = None) -> None:
        self.file_size = 1024
        self.file_name = name


class FakeMessage:
    def __init__(
        self,
        message_id: int = 1,
        *,
        text: str | None = None,
        caption: str | None = None,
        kind: str | None = None,
        outgoing: bool = False,
        reply_to: FakeMessage | None = None,
        chat_id: int = USER,
        chat_kind: str = "PRIVATE",
        sender: FakeUser | None = None,
    ) -> None:
        self.id = message_id
        self.text = text
        self.caption = caption
        self.outgoing = outgoing
        self.reply_to_message = reply_to
        self.chat = FakeChat(chat_id, kind=chat_kind)
        self.from_user = sender
        self.media = object() if kind else None
        for attribute in ("photo", "video", "audio", "document"):
            setattr(self, attribute, None)
        if kind == "photo":
            self.photo = FakeDownload()
        elif kind == "video":
            self.video = FakeDownload(name="clip.mp4")


class FakeUserClient:
    """The user's client: it downloads, and it speaks only in Saved Messages."""

    def __init__(self, *, fail_download: bool = False) -> None:
        self.messages: list[tuple[int, str, str | None]] = []
        self.downloads: list[int] = []
        self.fail_download = fail_download

    async def download_media(self, message: FakeMessage, *, file_name: str) -> str:
        if self.fail_download:
            raise RuntimeError("download failed")
        self.downloads.append(message.id)
        from pathlib import Path

        target = Path(file_name) / f"file-{message.id}"
        target.write_bytes(b"x" * 16)
        return str(target)

    async def send_message(self, chat_id: object, text: str, **_kwargs: object) -> None:
        self.messages.append(("message", str(chat_id), text))

    async def send_photo(self, chat_id: object, photo: object, caption: str | None = None) -> None:
        self.messages.append(("photo", str(chat_id), caption))

    async def send_video(self, chat_id: object, video: object, caption: str | None = None) -> None:
        self.messages.append(("video", str(chat_id), caption))

    async def send_audio(self, chat_id: object, audio: object, caption: str | None = None) -> None:
        self.messages.append(("audio", str(chat_id), caption))

    async def send_document(
        self, chat_id: object, document: object, caption: str | None = None
    ) -> None:
        self.messages.append(("document", str(chat_id), caption))


@pytest.fixture
def using(settings, monkeypatch, clean, tmp_path):
    """Test settings, an emptied database, and a scratch area of our own."""
    monkeypatch.setattr(paths, "DATA_DIR", tmp_path)
    monkeypatch.setattr(paths, "TMP_DIR", tmp_path / "tmp")
    monkeypatch.setattr(config, "get_settings", lambda: settings)
    timer.forget()
    return settings


async def _register(user_id: int = USER) -> None:
    async with db_module.session() as db:
        await users.upsert(db, user_id=user_id, first_name="T")


# ── AC-8: the magic word ────────────────────────────────────────────────


async def test_ac8_replying_the_magic_word_saves_the_media_to_saved_messages(using) -> None:
    await _register()
    client = FakeUserClient()
    media_message = FakeMessage(2, kind="video")
    reply = FakeMessage(3, text="WOW", outgoing=True, reply_to=media_message)

    outcome = await magic.handle(client, reply, settings=using, user_id=USER)

    assert outcome == catalog.MAGIC_SAVED
    assert client.downloads == [2], "the replied-to message is what was downloaded"
    assert client.messages == [("video", "me", catalog.MAGIC_SAVED)]


async def test_ac8_the_magic_word_is_case_insensitive(using) -> None:
    await _register()
    client = FakeUserClient()

    await magic.handle(
        client,
        FakeMessage(3, text="wOw", outgoing=True, reply_to=FakeMessage(2, kind="photo")),
        settings=using,
        user_id=USER,
    )

    assert client.downloads == [2]


async def test_a_changed_word_is_the_one_that_works(using) -> None:
    """FR-5.2 with FR-14: after a change, the old word does nothing and the new one works."""
    await _register()
    async with db_module.session() as db:
        await prefs.set_magic_word(db, USER, "go")

    client = FakeUserClient()
    target = FakeMessage(2, kind="photo")
    ignored = await magic.handle(
        client,
        FakeMessage(3, text="WOW", outgoing=True, reply_to=target),
        settings=using,
        user_id=USER,
    )
    kept = await magic.handle(
        client,
        FakeMessage(4, text="go", outgoing=True, reply_to=target),
        settings=using,
        user_id=USER,
    )

    assert ignored is None, "the default no longer applies to this user"
    assert kept == catalog.MAGIC_SAVED
    assert client.downloads == [2]


async def test_another_users_messages_are_never_considered(using) -> None:
    """FR-5.3: only the user's own outgoing messages. An incoming `WOW` is not a command."""
    await _register()
    client = FakeUserClient()
    incoming = FakeMessage(3, text="WOW", outgoing=False, reply_to=FakeMessage(2, kind="photo"))

    assert await magic.handle(client, incoming, settings=using, user_id=USER) is None
    assert client.messages == []


async def test_a_sentence_containing_the_word_is_not_the_word(using) -> None:
    await _register()
    client = FakeUserClient()

    outcome = await magic.handle(
        client,
        FakeMessage(
            3, text="wow this is great", outgoing=True, reply_to=FakeMessage(2, kind="photo")
        ),
        settings=using,
        user_id=USER,
    )

    assert outcome is None
    assert client.messages == [], "and it is left alone silently"


async def test_the_word_without_a_reply_explains_how_to_use_it(using) -> None:
    """FR-5.4."""
    await _register()
    client = FakeUserClient()

    outcome = await magic.handle(
        client, FakeMessage(3, text="WOW", outgoing=True), settings=using, user_id=USER
    )

    assert outcome == catalog.MAGIC_NO_REPLY.format(word="WOW")
    assert client.messages == [("message", "me", catalog.MAGIC_NO_REPLY.format(word="WOW"))]
    assert client.downloads == []


async def test_the_word_replied_to_a_text_message_explains_it_needs_media(using) -> None:
    await _register()
    client = FakeUserClient()

    outcome = await magic.handle(
        client,
        FakeMessage(3, text="WOW", outgoing=True, reply_to=FakeMessage(2, text="hello")),
        settings=using,
        user_id=USER,
    )

    assert outcome == catalog.MAGIC_NO_MEDIA.format(word="WOW")
    assert client.downloads == []


async def test_a_failed_save_is_reported_rather_than_silent(using) -> None:
    await _register()
    client = FakeUserClient(fail_download=True)

    outcome = await magic.handle(
        client,
        FakeMessage(3, text="WOW", outgoing=True, reply_to=FakeMessage(2, kind="photo")),
        settings=using,
        user_id=USER,
    )

    assert outcome == catalog.MAGIC_FAILED.format(reason="RuntimeError")
    assert client.messages[0][2] == catalog.MAGIC_FAILED.format(reason="RuntimeError")


async def test_a_read_failure_falls_back_to_the_default_word(using) -> None:
    """A preference is not a permission: it must never break the feature it configures."""

    class Exploding:
        async def get(self, *_args: object, **_kwargs: object) -> object:
            raise RuntimeError("database is down")

    assert await prefs.magic_word(Exploding(), using, USER) == using.ux.default_magic_word
    assert await prefs.timer_media(Exploding(), using, USER) is True


# ── AC-9: TimerMedia ────────────────────────────────────────────────────


async def test_ac9_an_incoming_private_photo_is_saved_with_a_sender_caption(using) -> None:
    await _register()
    client = FakeUserClient()
    message = FakeMessage(
        7,
        kind="photo",
        caption="look at this",
        sender=FakeUser(SENDER, username="stranger"),
        chat_id=SENDER,
    )

    outcome = await timer.handle(client, message, settings=using, user_id=USER)

    assert outcome == catalog.TIMER_SAVED
    kind, chat, caption = client.messages[0]
    assert (kind, chat) == ("photo", "me")
    assert "@stranger" in caption and str(SENDER) in caption and "look at this" in caption


async def test_a_private_video_is_saved_too(using) -> None:
    await _register()
    client = FakeUserClient()

    outcome = await timer.handle(
        client,
        FakeMessage(7, kind="video", sender=FakeUser(SENDER), chat_id=SENDER),
        settings=using,
        user_id=USER,
    )

    assert outcome == catalog.TIMER_SAVED
    assert client.messages[0][0] == "video"


async def test_ac9_with_the_toggle_off_nothing_is_saved(using) -> None:
    await _register()
    async with db_module.session() as db:
        await prefs.set_timer_media(db, USER, False)
    client = FakeUserClient()

    outcome = await timer.handle(
        client,
        FakeMessage(7, kind="photo", sender=FakeUser(SENDER), chat_id=SENDER),
        settings=using,
        user_id=USER,
    )

    assert outcome is None
    assert client.messages == []


async def test_the_default_is_on(using) -> None:
    """FR-6.1: enabled unless the user says otherwise."""
    await _register()

    async with db_module.session() as db:
        assert await prefs.timer_media(db, using, USER) is True


async def test_switching_the_magic_word_does_not_switch_auto_save_on(using) -> None:
    """One preference write must not resurrect another (they share a row)."""
    await _register()
    async with db_module.session() as db:
        await prefs.set_timer_media(db, USER, False)
        await prefs.set_magic_word(db, USER, "go")

        assert await prefs.timer_media(db, using, USER) is False


async def test_the_users_own_media_is_not_auto_saved(using) -> None:
    """FR-6.2: incoming from *other* users. Saving your own outgoing photos would be absurd."""
    await _register()
    client = FakeUserClient()

    outcome = await timer.handle(
        client,
        FakeMessage(
            7,
            kind="photo",
            outgoing=True,
            sender=FakeUser(USER),
            chat_id=SENDER,
        ),
        settings=using,
        user_id=USER,
    )

    assert outcome is None


async def test_a_text_message_is_not_auto_saved(using) -> None:
    await _register()
    client = FakeUserClient()

    outcome = await timer.handle(
        client,
        FakeMessage(7, text="hi", sender=FakeUser(SENDER), chat_id=SENDER),
        settings=using,
        user_id=USER,
    )

    assert outcome is None


async def test_a_group_photo_is_not_auto_saved(using) -> None:
    """FR-6.2 says private: a photo in a group is not something to keep without being asked."""
    await _register()
    client = FakeUserClient()

    outcome = await timer.handle(
        client,
        FakeMessage(
            7, kind="photo", sender=FakeUser(SENDER), chat_id=SENDER, chat_kind="SUPERGROUP"
        ),
        settings=using,
        user_id=USER,
    )

    assert outcome is None


async def test_the_same_media_is_never_saved_twice(using) -> None:
    """A reconnect can repeat an update; the user should not get two copies."""
    await _register()
    client = FakeUserClient()
    message = FakeMessage(7, kind="photo", sender=FakeUser(SENDER), chat_id=SENDER)

    first = await timer.handle(client, message, settings=using, user_id=USER)
    second = await timer.handle(client, message, settings=using, user_id=USER)

    assert first == catalog.TIMER_SAVED
    assert second is None
    assert len(client.messages) == 1


async def test_a_failed_auto_save_is_silent_and_keeps_the_client_alive(using) -> None:
    await _register()
    client = FakeUserClient(fail_download=True)

    outcome = await timer.handle(
        client,
        FakeMessage(7, kind="photo", sender=FakeUser(SENDER), chat_id=SENDER),
        settings=using,
        user_id=USER,
    )

    assert outcome is None, "there is nobody to tell: the media simply could not be saved"
    assert client.messages == []
