"""Bot assembly and lifecycle.

The order inside :func:`run` is the whole point of the module: the fast event loop has to
be installed before any event loop exists, and logging before anything can log — including
the client, which logs while it is being constructed.

What the loop then *does* lives in :mod:`app.runtime`: the client, the session pool,
the worker pool and the scheduler, started and stopped in the one order that leaves
nothing mid-flight. This module is the synchronous edge of that, which is why it is
separate — ``asyncio.run`` is not the place to make a start-up decision.
"""

from __future__ import annotations

import asyncio
import contextlib

from app import capabilities, runtime


def _install_fast_event_loop() -> bool:
    """Install uvloop on Unix, or winloop through winuvloop on Windows.

    Returns whether one was installed rather than raising: the loop is an optimisation, so
    a platform with neither keeps the standard one. It has to happen here, before
    ``asyncio.run`` creates a loop, or it silently has no effect. ``install()`` is itself
    deprecated and goes away with asyncio policies in 3.16, where the replacement is
    ``loop_factory``.
    """
    module = capabilities.fast_event_loop()
    if module is None:
        return False

    module.install()
    return True


def run() -> int:
    """``python main.py run``: install the fast loop, then serve until interrupted."""
    _install_fast_event_loop()
    with contextlib.suppress(KeyboardInterrupt):
        return asyncio.run(runtime.serve())
    return 0
