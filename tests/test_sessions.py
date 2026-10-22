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
    settings = SimpleNamespace(
        session_max_live=2,
        session_idle_seconds=10,
        session_idle_pro_seconds=100,
        session_idle_premium_seconds=1000,
        session_idle_for=lambda tier: {"pro": 100, "premium": 1000}.get(tier, 10),
    )
    fake_handler = SimpleNamespace(register_user_handlers=lambda _client: None)

    async def resolve_tiers(user_ids: list[int]) -> dict[int, str]:
        return {user_id: "free" for user_id in user_ids}

    monkeypatch.setattr(sessions, "_active_clients", {})
    monkeypatch.setattr(sessions, "_last_used", {})
    monkeypatch.setattr(sessions, "_leases", {})
    monkeypatch.setattr(sessions, "_tiers", {})
    monkeypatch.setattr(sessions, "_resolve_tiers", resolve_tiers)
    monkeypatch.setattr(sessions.config, "get_settings", lambda: settings)
    monkeypatch.setattr(sessions, "build_client", lambda user_id: clients.setdefault(user_id, _FakeClient(user_id)))
    monkeypatch.setattr("handler.handler.get_handler", lambda: fake_handler)
    yield clients, settings
    sessions._active_clients.clear()
    sessions._last_used.clear()
    sessions._leases.clear()
    sessions._tiers.clear()


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


def test_leases_are_reference_counted() -> None:
    sessions.acquire(5)
    sessions.acquire(5)

    sessions.release(5)
    assert sessions.is_leased(5)

    sessions.release(5)
    assert not sessions.is_leased(5)


@pytest.mark.asyncio
async def test_leased_client_is_not_evicted(session_state, monkeypatch: pytest.MonkeyPatch) -> None:
    _clients, _settings = session_state
    assert await sessions.start_client(1)
    sessions._last_used[1] = 10.0
    monkeypatch.setattr(sessions.time, "monotonic", lambda: 25.0)

    sessions.acquire(1)
    assert await sessions.evict_idle_clients() == 0
    assert sessions.active_count() == 1

    # Releasing stamps the clock (the job just used the client), so the sweep
    # must wait a full idle period again before dropping it.
    sessions.release(1)
    assert await sessions.evict_idle_clients() == 0

    monkeypatch.setattr(sessions.time, "monotonic", lambda: 100.0)
    assert await sessions.evict_idle_clients() == 1


@pytest.mark.asyncio
async def test_leased_client_is_not_lru_evicted(session_state) -> None:
    clients, _settings = session_state
    assert await sessions.start_client(1)
    assert await sessions.start_client(2)
    sessions._last_used[1] = 1.0  # oldest, but a job holds it
    sessions.acquire(1)

    assert await sessions.start_client(3)

    assert clients[1].disconnects == 0
    assert clients[2].disconnects == 1
    assert sessions.get_client(3) is clients[3]
    sessions.release(1)


@pytest.mark.asyncio
async def test_capacity_eviction_prefers_free_over_paying_tiers(
    session_state, monkeypatch: pytest.MonkeyPatch
) -> None:
    clients, _settings = session_state
    tiers = {1: "free", 2: "premium"}

    async def by_tier(user_ids: list[int]) -> dict[int, str]:
        return {user_id: tiers[user_id] for user_id in user_ids}

    monkeypatch.setattr(sessions, "_resolve_tiers", by_tier)
    assert await sessions.start_client(1)
    assert await sessions.start_client(2)
    sessions._last_used[1] = 100.0  # free, but used recently
    sessions._last_used[2] = 1.0  # premium, idle longest

    assert await sessions.start_client(3)

    assert clients[1].disconnects == 1  # free goes first despite being newer
    assert clients[2].disconnects == 0
    assert sessions.get_client(3) is clients[3]


@pytest.mark.asyncio
async def test_capacity_eviction_falls_back_to_the_oldest_premium(
    session_state, monkeypatch: pytest.MonkeyPatch
) -> None:
    clients, _settings = session_state
    tiers = {1: "premium", 2: "premium"}

    async def by_tier(user_ids: list[int]) -> dict[int, str]:
        return {user_id: tiers[user_id] for user_id in user_ids}

    monkeypatch.setattr(sessions, "_resolve_tiers", by_tier)
    assert await sessions.start_client(1)
    assert await sessions.start_client(2)
    sessions._last_used[1] = 1.0
    sessions._last_used[2] = 100.0

    assert await sessions.start_client(3)  # admission still succeeds

    assert clients[1].disconnects == 1  # oldest premium
    assert clients[2].disconnects == 0


@pytest.mark.asyncio
async def test_idle_timeout_follows_the_tier(session_state, monkeypatch: pytest.MonkeyPatch) -> None:
    _clients, _settings = session_state

    async def pro_tiers(user_ids: list[int]) -> dict[int, str]:
        return {user_id: "pro" for user_id in user_ids}

    monkeypatch.setattr(sessions, "_resolve_tiers", pro_tiers)
    assert await sessions.start_client(1)
    sessions._last_used[1] = 10.0

    monkeypatch.setattr(sessions.time, "monotonic", lambda: 60.0)  # 50 s idle
    assert await sessions.evict_idle_clients() == 0  # free would be gone at 10 s

    monkeypatch.setattr(sessions.time, "monotonic", lambda: 200.0)  # 190 s idle
    assert await sessions.evict_idle_clients() == 1


@pytest.mark.asyncio
async def test_touch_keeps_a_client_alive(session_state, monkeypatch: pytest.MonkeyPatch) -> None:
    _clients, _settings = session_state
    assert await sessions.start_client(1)
    sessions._last_used[1] = 10.0
    monkeypatch.setattr(sessions.time, "monotonic", lambda: 25.0)

    sessions.touch(1)

    assert await sessions.evict_idle_clients() == 0
    assert sessions.active_count() == 1


@pytest.mark.asyncio
async def test_release_refreshes_the_idle_timer(session_state, monkeypatch: pytest.MonkeyPatch) -> None:
    _clients, _settings = session_state
    assert await sessions.start_client(1)
    sessions._last_used[1] = 10.0
    monkeypatch.setattr(sessions.time, "monotonic", lambda: 500.0)  # long job

    sessions.acquire(1)
    sessions.release(1)  # the job just used the client

    assert await sessions.evict_idle_clients() == 0
    assert sessions.active_count() == 1


def test_touch_ignores_users_without_a_live_client() -> None:
    sessions.touch(999)  # must not create a timestamp

    assert 999 not in sessions._last_used


def test_session_file_permissions_are_restricted(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = tmp_path / "user.session"
    target.write_bytes(b"session")
    monkeypatch.setattr(sessions, "session_file", lambda _user_id: target)

    sessions.secure_session_file(42)

    if os.name != "nt":
        assert target.stat().st_mode & 0o777 == 0o600
