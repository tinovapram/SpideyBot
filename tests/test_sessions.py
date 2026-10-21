from __future__ import annotations

import os
from types import SimpleNamespace

import pytest

pytest.importorskip("telethon")

from core import sessions


class _FakeClient:
    def __init__(self, user_id: int) -> None:
        self.user_id = user_id
        self.connected = False
        self.disconnects = 0

    async def connect(self) -> None:
        self.connected = True

    async def is_user_authorized(self) -> bool:
        return True

    async def disconnect(self) -> None:
        self.connected = False
        self.disconnects += 1

    def is_connected(self) -> bool:
        return self.connected


@pytest.fixture
def session_state(monkeypatch: pytest.MonkeyPatch):
    clients: dict[int, _FakeClient] = {}
    settings = SimpleNamespace(session_max_live=2, session_idle_seconds=10)
    fake_handler = SimpleNamespace(register_user_handlers=lambda _client: None)

    monkeypatch.setattr(sessions, "_active_clients", {})
    monkeypatch.setattr(sessions, "_last_used", {})
    monkeypatch.setattr(sessions.config, "get_settings", lambda: settings)
    monkeypatch.setattr(sessions, "build_client", lambda user_id: clients.setdefault(user_id, _FakeClient(user_id)))
    monkeypatch.setattr("handler.handler.get_handler", lambda: fake_handler)
    yield clients, settings
    sessions._active_clients.clear()
    sessions._last_used.clear()


@pytest.mark.asyncio
async def test_start_client_reuses_connected_client(session_state) -> None:
    clients, _settings = session_state

    assert await sessions.start_client(1)
    first = clients[1]
    assert await sessions.start_client(1)

    assert sessions.get_client(1) is first
    assert first.disconnects == 0


@pytest.mark.asyncio
async def test_start_client_evicts_least_recently_used(session_state) -> None:
    clients, _settings = session_state

    assert await sessions.start_client(1)
    assert await sessions.start_client(2)
    sessions._last_used[1] = 1.0
    sessions._last_used[2] = 2.0

    assert await sessions.start_client(3)

    assert clients[1].disconnects == 1
    assert sessions.active_count() == 2
    assert sessions.get_client(3) is clients[3]


@pytest.mark.asyncio
async def test_idle_clients_are_evicted(session_state, monkeypatch: pytest.MonkeyPatch) -> None:
    _clients, _settings = session_state
    assert await sessions.start_client(1)
    sessions._last_used[1] = 10.0
    monkeypatch.setattr(sessions.time, "monotonic", lambda: 25.0)

    assert await sessions.evict_idle_clients() == 1
    assert sessions.active_count() == 0


def test_session_file_permissions_are_restricted(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = tmp_path / "user.session"
    target.write_bytes(b"session")
    monkeypatch.setattr(sessions, "session_file", lambda _user_id: target)

    sessions.secure_session_file(42)

    if os.name != "nt":
        assert target.stat().st_mode & 0o777 == 0o600
