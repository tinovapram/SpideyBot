"""Handler-level guards that run before any work is queued."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import handler.handler as handler_module
from core.ratelimit import RateLimiter
from handler.handler import Handler


class FakeClient:
    def __init__(self) -> None:
        self.messages: list[tuple[int, str]] = []

    async def send_message(self, chat_id: int, text: str, **_kwargs: object) -> object:
        self.messages.append((chat_id, text))
        return SimpleNamespace(id=99)


class FakeManager:
    def __init__(self) -> None:
        self.calls = 0

    async def add_task(self, *_args: object, **_kwargs: object) -> None:
        self.calls += 1
        raise AssertionError("add_task must not be called")


def _event(client: FakeClient, text: str = "/dl http://example.com/file") -> SimpleNamespace:
    return SimpleNamespace(
        sender_id=42,
        chat_id=7,
        message=SimpleNamespace(id=5),
        client=client,
        text=text,
    )


async def test_private_url_is_rejected_before_queueing() -> None:
    client = FakeClient()
    manager = FakeManager()
    handler = Handler(client=client, manager=manager)  # type: ignore[arg-type]

    await handler._enqueue_download(_event(client), "http://127.0.0.1/secret")

    assert manager.calls == 0
    assert client.messages
    assert "Private or local" in client.messages[0][1]


async def test_rate_limited_request_is_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    client = FakeClient()
    handler = Handler(client=client, manager=FakeManager())  # type: ignore[arg-type]
    monkeypatch.setattr(
        handler_module,
        "get_limiter",
        lambda: RateLimiter(capacity=0, refill_per_sec=0.0),
    )

    limited = await handler._rate_limited(_event(client))

    assert limited is True
    assert "Too many requests" in client.messages[0][1]


async def test_user_client_activity_refreshes_the_session_idle_timer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    touched: list[int] = []
    calls: list[int] = []

    async def fake_magic(event) -> None:
        calls.append(event.sender_id)

    monkeypatch.setattr(handler_module.sessions, "touch", lambda user_id: touched.append(user_id))
    handler = Handler(client=FakeClient(), manager=FakeManager())  # type: ignore[arg-type]
    monkeypatch.setattr(handler, "magic_user_handler", fake_magic)

    class RecordingClient:
        def __init__(self) -> None:
            self.handlers: list[object] = []

        def add_event_handler(self, callback, _event_filter) -> None:
            self.handlers.append(callback)

    client = RecordingClient()
    handler.register_user_handlers(client)  # type: ignore[arg-type]

    magic = client.handlers[4]  # dl, dt, cancel, status, magic, timermedia
    await magic(SimpleNamespace(sender_id=42))  # type: ignore[operator]

    assert touched == [42]
    assert calls == [42]
