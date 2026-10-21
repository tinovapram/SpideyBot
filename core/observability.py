"""Small HTTP endpoints for container health and Prometheus scraping."""

from __future__ import annotations

import os
from collections.abc import Callable

from aiohttp import web
from sqlalchemy import text

from core.config import get_settings
from core.metrics import get_metrics
from utils.paths import READY_FILE

_runner: web.AppRunner | None = None
_probes: dict[str, Callable[[], bool]] = {}
_components = {
    "database": False,
    "telegram": False,
    "worker": False,
    "scheduler": False,
}


def set_component_status(component: str, healthy: bool) -> None:
    """Update an internal readiness component without logging secrets."""
    if component not in _components:
        raise ValueError(f"Unknown readiness component: {component}")
    _components[component] = healthy


def component_status() -> dict[str, bool]:
    return dict(_components)


def register_probe(component: str, probe: Callable[[], bool]) -> None:
    """Register a live probe that ``/readyz`` evaluates on every request."""
    _probes[component] = probe


async def _database_healthy() -> bool:
    """Ping the database so readiness reflects the current state, not startup."""
    from core import db

    try:
        async with db.engine().connect() as connection:
            await connection.execute(text("SELECT 1"))
    except Exception:
        return False
    return True


def _ready() -> bool:
    try:
        pid = int(READY_FILE.read_text(encoding="ascii"))
        os.kill(pid, 0)
        return True
    except (OSError, ValueError):
        return False


async def _live(_request: web.Request) -> web.Response:
    return web.Response(text="ok\n")


async def _ready_endpoint(_request: web.Request) -> web.Response:
    if not _ready():
        return web.Response(status=503, text="not ready\n")

    components = component_status()
    if "database" in components:
        components["database"] = await _database_healthy()
    for name, probe in _probes.items():
        try:
            components[name] = bool(probe())
        except Exception:
            components[name] = False

    if not all(components.values()):
        return web.Response(status=503, text="not ready\n")
    return web.Response(text="ready\n")


async def _metrics(_request: web.Request) -> web.Response:
    return web.Response(text=get_metrics().prometheus(), content_type="text/plain")


async def start_server() -> web.AppRunner:
    """Start liveness, readiness, and metrics endpoints."""
    global _runner
    if _runner is not None:
        return _runner
    app = web.Application()
    app.router.add_get("/livez", _live)
    app.router.add_get("/readyz", _ready_endpoint)
    app.router.add_get("/metrics", _metrics)
    _runner = web.AppRunner(app)
    await _runner.setup()
    settings = get_settings()
    site = web.TCPSite(_runner, settings.metrics_host, settings.metrics_port)
    await site.start()
    return _runner


async def stop_server() -> None:
    global _runner
    if _runner is not None:
        await _runner.cleanup()
        _runner = None
