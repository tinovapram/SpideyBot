"""`/stop` disconnects the live client without deleting the stored login."""

from __future__ import annotations

import re
from types import SimpleNamespace
from typing import Any

import pytest

import handler.login as login_module


class _FakeBot:
    """Captures the handlers registered with ``@bot.on(...)``."""

    def __init__(self) -> None:
        self.handlers: dict[str, Any] = {}
        self.patterns: dict[str, str] = {}

    def on(self, event_filter: Any):
        def decorator(func: Any) -> Any:
            self.handlers[func.__name__] = func
            self.patterns[func.__name__] = str(getattr(event_filter, "pattern", "") or "")
            return func

        return decorator


class _FakeClient:
    def __init__(self) -> None:
        self.messages: list[str] = []

    async def send_message(self, chat_id, text, **_kwargs):
        self.messages.append(text)
        return SimpleNamespace(id=1)


def _event() -> SimpleNamespace:
    return SimpleNamespace(sender_id=42, chat_id=7, client=_FakeClient())


@pytest.fixture
def bot() -> _FakeBot:
    instance = _FakeBot()
    login_module.register_login_handlers(instance)
    return instance


@pytest.fixture
def spies(monkeypatch: pytest.MonkeyPatch):
    calls = {"stopped": [], "removed": []}

    monkeypatch.setattr(login_module.sessions, "get_client", lambda _uid: None)
    monkeypatch.setattr(login_module.sessions, "has_session", lambda _uid: False)
    monkeypatch.setattr(login_module.sessions, "is_leased", lambda _uid: False)

    async def stop_client(user_id):
        calls["stopped"].append(user_id)
        return True

    async def remove_session(user_id):
        calls["removed"].append(user_id)
        return True

    monkeypatch.setattr(login_module.sessions, "stop_client", stop_client)
    monkeypatch.setattr(login_module.sessions, "remove_session", remove_session)
    return calls


def test_pattern_matches_stop_but_not_longer_words(
    bot: _FakeBot, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake_new_message(**kwargs: Any) -> SimpleNamespace:
        return SimpleNamespace(pattern=kwargs.get("pattern"))

    monkeypatch.setattr(login_module.events, "NewMessage", fake_new_message)
    capture = _FakeBot()
    login_module.register_login_handlers(capture)
    pattern = capture.patterns["stop_handler"]

    assert re.match(pattern, "/stop")
    assert re.match(pattern, "/stop@SpideyBot")
    assert not re.match(pattern, "/stopwatch")
    assert "stop_handler" in bot.handlers


async def test_stop_without_any_login_points_to_login(bot: _FakeBot, spies) -> None:
    event = _event()

    await bot.handlers["stop_handler"](event)

    assert "No active session" in event.client.messages[0]
    assert "/login" in event.client.messages[0]
    assert spies["stopped"] == []


async def test_stop_with_saved_login_points_to_start(
    bot: _FakeBot, spies, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(login_module.sessions, "has_session", lambda _uid: True)
    event = _event()

    await bot.handlers["stop_handler"](event)

    assert "/start" in event.client.messages[0]
    assert spies["stopped"] == []


async def test_stop_disconnects_and_keeps_the_session(
    bot: _FakeBot, spies, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(login_module.sessions, "get_client", lambda _uid: object())
    event = _event()

    await bot.handlers["stop_handler"](event)

    assert spies["stopped"] == [42]
    assert spies["removed"] == []  # /stop must not delete the session
    assert "Session stopped" in event.client.messages[0]
    assert "/start" in event.client.messages[0]
    assert "/logout" in event.client.messages[0]


async def test_stop_refuses_while_a_download_holds_the_client(
    bot: _FakeBot, spies, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(login_module.sessions, "get_client", lambda _uid: object())
    monkeypatch.setattr(login_module.sessions, "is_leased", lambda _uid: True)
    event = _event()

    await bot.handlers["stop_handler"](event)

    assert spies["stopped"] == []
    assert "/cancel" in event.client.messages[0]
