"""Event-loop selection for supported platforms."""

from __future__ import annotations

import asyncio

import structlog

logger = structlog.get_logger(__name__)


def configure_event_loop() -> str:
    """Install winuvloop and return the selected backend name.

    ``winuvloop`` selects uvloop on POSIX platforms and winloop on Windows.
    A fallback keeps local development usable when the optional accelerator is
    unavailable, while production installations receive it from pyproject.toml.
    """
    try:
        import winuvloop
    except ImportError:
        backend = type(asyncio.get_event_loop_policy()).__name__
        logger.warning("event_loop_acceleration_unavailable", backend=backend)
        return backend

    winuvloop.install()
    backend_name = getattr(winuvloop, "backend_name", None)
    backend = backend_name() if callable(backend_name) else "winuvloop"
    logger.info("event_loop_configured", backend=backend)
    return str(backend)
