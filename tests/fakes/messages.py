"""Stand-ins for the Kurigram objects a handler receives (plan section 5).

Deliberately not mocks. A handler reads ``from_user``, ``command`` and calls
``reply_text``; a small object with exactly those attributes documents what the handler
actually depends on, and fails loudly when that dependency grows without the fake knowing.
"""

from __future__ import annotations

from typing import Any


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
