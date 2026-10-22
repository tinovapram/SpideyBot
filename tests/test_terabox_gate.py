"""TeraBox access gate: a user needs their own valid `ndus` cookie (admins exempt)."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

import handler.handler as handler_module
from core import cookies as cookie_store
from core import worker
from handler.handler import Handler

VALID = "ndus=" + "a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6; browserid=xyz"
DUMMY_SESSION = cast(AsyncSession, SimpleNamespace())


class _FakeClient:
    def __init__(self) -> None:
        self.messages: list[str] = []

    async def send_message(self, chat_id: int, text: str, **_kwargs: Any) -> Any:
        self.messages.append(text)
        return SimpleNamespace(id=99)


class _FakeManager:
    def __init__(self) -> None:
        self.calls = 0

    async def add_task(self, *_args: Any, **_kwargs: Any) -> Any:
        self.calls += 1
        raise AssertionError("add_task must not be called")


class _SessionScope:
    async def __aenter__(self) -> Any:
        return SimpleNamespace()

    async def __aexit__(self, *_args: object) -> None:
        return None


def _event(client: _FakeClient) -> SimpleNamespace:
    return SimpleNamespace(
        sender_id=42,
        chat_id=7,
        message=SimpleNamespace(id=5),
        client=client,
        text="",
    )


# ── cookie validation ────────────────────────────────────────────


def test_parse_ndus_finds_the_value() -> None:
    assert cookie_store.parse_ndus(VALID) == "a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6"
    assert cookie_store.parse_ndus("browserid=x; NDUS=abc; other=1") == "abc"
    assert cookie_store.parse_ndus("browserid=x") is None
    assert cookie_store.parse_ndus("ndus=") is None


@pytest.mark.parametrize(
    ("cookie", "expected"),
    [
        ("", "empty"),
        ("browserid=x", "non-empty"),
        ("ndus=", "non-empty"),
        ("ndus=short", "truncated"),
        (VALID, None),
    ],
)
def test_validate_cookie_rejects_unusable_values(cookie: str, expected: str | None) -> None:
    problem = cookie_store.validate_cookie(cookie)

    if expected is None:
        assert problem is None
    else:
        assert problem is not None
        assert expected in problem


async def test_has_valid_cookie_treats_unreadable_cookies_as_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def raises(_session: Any, _user_id: int) -> str:
        raise RuntimeError("corrupt ciphertext")

    monkeypatch.setattr(cookie_store, "get_cookie", raises)

    assert await cookie_store.has_valid_cookie(DUMMY_SESSION, 42) is False


async def test_has_valid_cookie_checks_the_stored_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def short(_session: Any, _uid: int) -> str:
        return "ndus=short"

    monkeypatch.setattr(cookie_store, "get_cookie", short)
    assert await cookie_store.has_valid_cookie(DUMMY_SESSION, 42) is False

    async def valid(_session: Any, _uid: int) -> str:
        return VALID

    monkeypatch.setattr(cookie_store, "get_cookie", valid)
    assert await cookie_store.has_valid_cookie(DUMMY_SESSION, 42) is True


# ── handler gate ─────────────────────────────────────────────────


@pytest.fixture
def gate(monkeypatch: pytest.MonkeyPatch):
    """Handler with the DB session and cookie lookup stubbed out."""
    valid = {"value": False}

    async def has_valid(_session: Any, _user_id: int) -> bool:
        return valid["value"]

    monkeypatch.setattr(handler_module, "session_scope", _SessionScope)
    monkeypatch.setattr(handler_module.cookies, "has_valid_cookie", has_valid)
    client = _FakeClient()
    return Handler(client=client, manager=_FakeManager()), client, valid  # type: ignore[arg-type]


async def test_terabox_link_without_a_cookie_is_gated(gate) -> None:
    handler, _client, _valid = gate

    blocked = await handler._terabox_cookie_missing(42, "https://www.terabox.com/s/abc")

    assert blocked is True


async def test_terabox_link_with_a_valid_cookie_passes(gate) -> None:
    handler, _client, valid = gate
    valid["value"] = True

    assert await handler._terabox_cookie_missing(42, "https://www.terabox.com/s/abc") is False


async def test_non_terabox_links_are_never_gated(gate) -> None:
    handler, _client, _valid = gate

    assert await handler._terabox_cookie_missing(42, "https://example.com/clip.mp4") is False


async def test_admins_are_exempt(gate, monkeypatch: pytest.MonkeyPatch) -> None:
    handler, _client, _valid = gate
    monkeypatch.setattr(handler_module, "is_admin", lambda _user_id: True)

    assert await handler._terabox_cookie_missing(42, "https://www.terabox.com/s/abc") is False


async def test_blocked_terabox_link_is_not_queued(
    gate, monkeypatch: pytest.MonkeyPatch
) -> None:
    handler, client, _valid = gate

    async def public_url(url: str) -> str:
        return url

    monkeypatch.setattr(handler_module, "validate_public_url", public_url)

    await handler._enqueue_download(
        _event(client), "https://www.terabox.com/s/abc"
    )

    assert handler.manager.calls == 0  # type: ignore[attr-defined]
    assert "TeraBox needs your own account" in client.messages[0]
    assert "/setting" in client.messages[0]


# ── worker gate (defence in depth) ───────────────────────────────


class _Status:
    def __init__(self) -> None:
        self.texts: list[str] = []

    async def edit(self, text: str, **_kwargs: Any) -> None:
        self.texts.append(text)


async def test_worker_refuses_a_terabox_job_without_a_valid_cookie(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job = SimpleNamespace(
        id=11, user_id=42, link="https://www.terabox.com/s/abc", site="terabox", attempts=1
    )
    # Even with a configured bot pool, a normal user needs their own cookie.
    manager = worker.DownloadManager(bot=object(), terabox_downloader=object())
    status = _Status()
    task = worker.DownloadTask(
        entry_id=job.id,
        user_id=job.user_id,
        link=job.link,
        site="terabox",
        tier="free",
        is_admin=False,
        event=worker._TaskEvent(None, chat_id=1, msg_id=2),
        status_msg=status,  # type: ignore[arg-type]
    )
    manager.active_tasks[job.id] = task

    async def no_cookie(_session: Any, _user_id: int) -> bool:
        return False

    monkeypatch.setattr(worker, "session_scope", _SessionScope)
    monkeypatch.setattr("core.cookies.has_valid_cookie", no_cookie)

    await manager._run_job(job)

    assert status.texts and "needs your own account" in status.texts[-1]
    assert job.id not in manager.active_tasks
