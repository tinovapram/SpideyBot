"""Stand-ins for the Kurigram objects a handler receives (plan section 5).

Deliberately not mocks. A handler reads ``from_user``, ``command`` and calls
``reply_text``; a small object with exactly those attributes documents what the handler
actually depends on, and fails loudly when that dependency grows without the fake knowing.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

#: How a status-message replacement is recorded (FR-7.9). Named because two places compare against
#: it: the recording client, and every test that needs to tell an edit from a send.
EDIT = "edit_message_text"


class FakeUser:
    def __init__(
        self,
        user_id: int,
        first_name: str = "Ada",
        username: str | None = None,
    ) -> None:
        self.id = user_id
        self.first_name = first_name
        self.username = username


class FakeMessage:
    """Captures what a handler sent, so a test can assert on it."""

    def __init__(
        self,
        user: FakeUser | None,
        command: tuple[str, ...] = ("start",),
        text: str | None = None,
    ) -> None:
        self.from_user = user
        # Pyrogram exposes the command as a list, e.g. ["start", "123456"] for a deep link.
        self.command = list(command)
        self.text = text
        self.replies: list[str] = []
        self.reply_markups: list[Any] = []

    async def reply_text(self, text: str, reply_markup: Any = None, **_kwargs: Any) -> None:
        self.replies.append(text)
        self.reply_markups.append(reply_markup)

    @property
    def reply(self) -> str:
        """The single reply this message received, asserting there was exactly one."""
        assert len(self.replies) == 1, f"expected one reply, got {len(self.replies)}"
        return self.replies[0]


class FakeClient:
    """A client that refuses to be used, for handlers that should not call it."""

    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"the handler unexpectedly used the client: {name}")


class SentMessage:
    """One thing that was sent, with the arguments it was sent with."""

    def __init__(self, method: str, kwargs: dict[str, Any], message_id: int) -> None:
        self.method = method
        self.kwargs = kwargs
        self.id = message_id
        self.message_ids = (message_id,)
        #: Which album this message belongs to, when it arrived in one. Kurigram returns one message
        #: per album item, so counting messages is not counting albums.
        self.group: int | None = None

    @property
    def text(self) -> str | None:
        return self.kwargs.get("text")

    @property
    def caption(self) -> str | None:
        caption = self.kwargs.get("caption")
        if caption is None and self.kwargs.get("media"):
            return getattr(self.kwargs["media"][0], "caption", None)
        return caption

    @property
    def path(self) -> str | None:
        """The file that was sent, whether it went alone or inside an album."""
        for key in ("photo", "video", "audio", "document"):
            value = self.kwargs.get(key)
            if value is not None:
                return str(value)
        media = self.kwargs.get("media")
        if media:
            return str(getattr(media[0], "media", media[0]))
        return None

    @property
    def media(self) -> list[Any]:
        return list(self.kwargs.get("media") or [])

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        shown = self.text or self.caption or self.path
        return f"<{self.method} {shown!r}>"


class RecordingClient:
    """A client that records what a job sent, and can be told to fail specific files.

    ``fail`` matches on the *filename*, because that is the identity a test can write down without
    knowing the path a staging directory happened to use.
    """

    def __init__(self, *, fail: set[str] | None = None) -> None:
        #: Everything the job put in the chat, in the order it happened. ``sent`` and ``edits`` are
        #: views over this, so "what did the user see, and in what order" stays answerable — a
        #: status replacement that arrived between two file sends must not be sorted to the end.
        self.events: list[SentMessage] = []
        self.fail = fail or set()
        self._next_id = 1000
        self._next_group = 0
        self.closed = False

    # ── what the tests read ──────────────────────────────────────────────

    @property
    def sent(self) -> list[SentMessage]:
        """The messages that delivered something, in order."""
        return [event for event in self.events if event.method != EDIT]

    @property
    def edits(self) -> list[SentMessage]:
        """FR-7.1's in-place status updates, in order."""
        return [event for event in self.events if event.method == EDIT]

    @property
    def status_texts(self) -> list[str]:
        """Only the status message's bodies, for asserting on FR-7's phases and rows."""
        return [event.text for event in self.edits if event.text]

    # ── the sender protocol delivery depends on ──────────────────────────

    async def send_photo(self, **kwargs: Any) -> SentMessage:
        return self._record("send_photo", kwargs)

    async def send_video(self, **kwargs: Any) -> SentMessage:
        return self._record("send_video", kwargs)

    async def send_audio(self, **kwargs: Any) -> SentMessage:
        return self._record("send_audio", kwargs)

    async def send_document(self, **kwargs: Any) -> SentMessage:
        return self._record("send_document", kwargs)

    async def send_media_group(self, **kwargs: Any) -> list[SentMessage]:
        """One message per item, which is what the real API returns for a group."""
        media = list(kwargs["media"])
        for item in media:
            name = Path(str(getattr(item, "media", item))).name
            if name in self.fail:
                raise RuntimeError(f"Telegram refused {name}")
        self._next_group += 1
        group = self._next_group
        messages: list[SentMessage] = []
        for item in media:
            self._next_id += 1
            message = SentMessage("send_media_group", {**kwargs, "media": [item]}, self._next_id)
            message.group = group
            messages.append(message)
        self.events.extend(messages)
        return messages

    async def send_message(self, **kwargs: Any) -> SentMessage:
        return self._record("send_message", kwargs)

    async def edit_message_text(self, **kwargs: Any) -> SentMessage:
        """An edit is not a send: it carries no files and no reply anchor.

        Recorded in the same event list for ordering, but excluded from ``sent`` — a test asking
        "did everything reply to the command?" (FR-8.4) must not be answered by an edit, which
        cannot reply to anything.
        """
        name = self._name_in(kwargs)
        if name is not None and name in self.fail:
            raise RuntimeError(f"Telegram refused {name}")
        message = SentMessage(EDIT, kwargs, int(kwargs.get("message_id") or 0))
        self.events.append(message)
        return message

    # ── what the tests read ──────────────────────────────────────────────

    def _record(self, method: str, kwargs: dict[str, Any]) -> SentMessage:
        name = self._name_in(kwargs)
        if name is not None and name in self.fail:
            raise RuntimeError(f"Telegram refused {name}")
        self._next_id += 1
        message = SentMessage(method, kwargs, self._next_id)
        self.events.append(message)
        return message

    @staticmethod
    def _name_in(kwargs: dict[str, Any]) -> str | None:
        for key in ("photo", "video", "audio", "document"):
            value = kwargs.get(key)
            if value is not None:
                return Path(str(value)).name
        media = kwargs.get("media")
        if media:
            return Path(str(getattr(media[0], "media", media[0]))).name
        return None

    @property
    def files(self) -> list[str]:
        """Every filename delivered, in order."""
        return [Path(message.path).name for message in self.sent if message.path]

    @property
    def groups(self) -> dict[int, list[str]]:
        """Each album's member filenames, so a test can talk about albums rather than messages."""
        albums: dict[int, list[str]] = {}
        for message in self.sent:
            if message.group is None or message.path is None:
                continue
            albums.setdefault(message.group, []).append(Path(message.path).name)
        return albums

    @property
    def texts(self) -> list[str]:
        """Every message the job produced, in the order the user saw them.

        Sends and status-message replacements are both in here, because a caller asking "what did
        the user end up seeing?" does not care which mechanism carried it. Order comes from
        ``events``, so a status replacement between two file sends stays between them — and FR-7.9's
        final body, which is always last, is what makes ``texts[-1]`` the job's result.
        """
        return [event.text for event in self.events if event.text]

    @property
    def messages_for(self, name: str) -> list[str]:
        """The texts of the messages that mention a filename, for AC-15's "named" requirement."""
        return [text for text in self.texts if name in text]
