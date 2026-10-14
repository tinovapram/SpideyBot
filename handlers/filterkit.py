"""Custom filters that survive a library whose calling convention moves.

``filters.create(func)`` does not wrap ``func`` it **installs it as ``__call__`` of a new ``Filter``
subclass**, so a filter is invoked as a bound method and the predicate receives three positional
arguments — ``(filter, client, update)``. That is what the library documents, and its own built-ins
are written that way (``async def all_filter(_, __, update)``).

It has not always been that way, and the difference is invisible until it fires: Kurigram has also
called custom filters as ``filter(client, update)``. A predicate written for one convention then
raises ``TypeError`` for *every* update its handler is attached to — which is how the settings
conversation became unreachable in production while every test passed, because a handler test calls
the handler and never the filter.

The update is the last positional argument under both conventions, so that is the only part relied
on here. Predicates are async on purpose: the library awaits a coroutine predicate in the event
loop, while a synchronous one is pushed through its executor thread.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from pyrogram import filters

#: A check over the incoming update — a ``Message`` for a message handler, a ``CallbackQuery`` for a
#: callback one. It never sees the filter or the client.
Predicate = Callable[[Any], Awaitable[bool]]


def message_filter(predicate: Predicate, name: str) -> filters.Filter:
    """A custom filter from a check over the update.

    ``name`` is required rather than derived, because the library falls back to ``func.__name__`` —
    which for a closure is ``"call"``, and that is what a traceback would show.
    """

    async def call(*args: Any) -> bool:
        return await predicate(args[-1])

    return filters.create(call, name)
