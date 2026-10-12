"""Bot assembly and lifecycle.

The order inside :func:`run` is the whole point of the module: uvloop has to be
installed before any event loop exists, and logging before anything can log —
including the client, which logs while it is being constructed.

What the loop then *does* lives in :mod:`app.runtime`: the client, the session pool,
the worker pool and the scheduler, started and stopped in the one order that leaves
nothing mid-flight. This module is the synchronous edge of that, which is why it is
separate — ``asyncio.run`` is not the place to make a start-up decision.
"""

from __future__ import annotations

import asyncio
import contextlib

from app import runtime


def _install_uvloop() -> bool:
    """Use uvloop if it is available.

    Unix-only, so on Windows this reports "not installed" rather than failing: the
    bot is developed on Windows and deployed on Linux.
    """
    try:
        import uvloop
    except ImportError:
        return False

    uvloop.install()
    return True


def run() -> int:
    """``python main.py run``: install the fast loop, then serve until interrupted."""
    _install_uvloop()
    with contextlib.suppress(KeyboardInterrupt):
        return asyncio.run(runtime.serve())
    return 0
