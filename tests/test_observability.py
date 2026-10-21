from __future__ import annotations

from typing import Any, cast

import pytest

from core import observability


async def _healthy() -> bool:
    return True


async def _unhealthy() -> bool:
    return False


async def _readyz() -> int:
    """Call the readiness handler; it ignores the request object."""
    return (await observability._ready_endpoint(cast(Any, None))).status


@pytest.fixture
def ready_components(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        observability,
        "_components",
        {"database": True, "telegram": True, "worker": True, "scheduler": True},
    )
    monkeypatch.setattr(observability, "_probes", {})
    monkeypatch.setattr(observability, "_ready", lambda: True)


async def test_readyz_ok_when_everything_is_healthy(
    ready_components: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(observability, "_database_healthy", _healthy)

    assert await _readyz() == 200


async def test_readyz_fails_when_the_database_drops(
    ready_components: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(observability, "_database_healthy", _unhealthy)

    assert await _readyz() == 503


async def test_readyz_fails_when_a_registered_probe_is_unhealthy(
    ready_components: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(observability, "_database_healthy", _healthy)
    monkeypatch.setattr(observability, "_probes", {"telegram": lambda: False})

    assert await _readyz() == 503
