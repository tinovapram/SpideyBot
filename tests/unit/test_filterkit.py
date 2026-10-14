"""The custom filters, evaluated the way the library evaluates them.

This file exists because the failure it guards is invisible from a handler test. The settings
conversation was unreachable in production — no TeraBox cookie and no magic word could be set —
while every test passed, because the tests call ``capture_input(client, message)`` directly and
nothing ever *evaluated* the filter that decides whether that handler should run at all.

How the library calls a custom filter is worth asserting rather than assuming, because the mechanism
is not what the name suggests: ``filters.create(func)`` returns a ``Filter`` **subclass whose
``__call__`` is ``func``**, and ``AndFilter`` evaluates it as ``self.base(client, update)`` — a
bound call, so the predicate receives three positional arguments, ``(filter, client, update)``. The
library's docstring says exactly that, and its own built-ins are written that way
(``async def all_filter(_, __, update)``). A predicate written for two arguments therefore raises
``TypeError`` for every update its handler is attached to.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from handlers import login as login_handler
from handlers import setting as setting_handler
from handlers.filterkit import message_filter
from tests.fakes.messages import FakeClient, FakeMessage, FakeUser

USER = 12345
OTHER_USER = 54321


@pytest.fixture
def message() -> FakeMessage:
    """A plain private message: not a command, with text to capture."""
    return FakeMessage(FakeUser(USER, "TestUser"), command=(), text="ndus=abc")


# ─────────────────────────────────────────────────────────────────────────────
# The filters the handlers are registered with
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_the_settings_filter_matches_only_a_sender_awaiting_input(
    message: FakeMessage,
) -> None:
    """Evaluated through the filter's own ``__call__``, which is the call that raised."""
    flt = setting_handler._awaiting_input_filter()

    with patch.dict(setting_handler._AWAITING, {USER: "cookie"}):
        assert await flt(FakeClient(), message) is True

    assert await flt(FakeClient(), message) is False, "nobody asked this user for anything"


@pytest.mark.asyncio
async def test_the_settings_filter_refuses_a_message_with_no_sender() -> None:
    """A channel post has no ``from_user``, and only the handler's types made that look safe."""
    flt = setting_handler._awaiting_input_filter()

    with patch.dict(setting_handler._AWAITING, {USER: "cookie"}):
        assert await flt(FakeClient(), FakeMessage(None, command=(), text="ndus=abc")) is False


@pytest.mark.asyncio
async def test_the_login_filter_matches_only_a_sender_in_the_conversation(
    message: FakeMessage, monkeypatch: pytest.MonkeyPatch
) -> None:
    flow = MagicMock()
    flow.is_awaiting_input.side_effect = lambda user_id: user_id == USER
    monkeypatch.setattr(login_handler, "get_flow", lambda: flow)

    flt = login_handler._step_filter()

    assert await flt(FakeClient(), message) is True
    stranger = FakeMessage(FakeUser(OTHER_USER, "Someone"), command=(), text="hello")
    assert await flt(FakeClient(), stranger) is False


@pytest.mark.asyncio
async def test_the_login_filter_leaves_commands_alone(
    message: FakeMessage, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A command during a login belongs to its own handler — ``/cancel`` aborts it."""
    flow = MagicMock()
    flow.is_awaiting_input.return_value = True
    monkeypatch.setattr(login_handler, "get_flow", lambda: flow)

    command = FakeMessage(FakeUser(USER), command=("cancel",), text="/cancel")

    assert await login_handler._step_filter()(FakeClient(), command) is False


# ─────────────────────────────────────────────────────────────────────────────
# The builder both filters are built with
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("call", ["filter, client, update", "client, update"])
async def test_the_builder_works_under_either_library_convention(call: str) -> None:
    """Kurigram has called custom filters both ways, and the update is last either way.

    The installed version installs the predicate as ``__call__``, so a filter is invoked as a bound
    method and the predicate sees three arguments; a version that wraps it in a generic ``__call__``
    would pass two. Both are exercised here, so neither a downgrade nor an upgrade can turn a filter
    into a silent ``TypeError`` — or, worse, a constant.
    """
    seen: list[Any] = []

    async def predicate(update: Any) -> bool:
        seen.append(update)
        return update == "the update"

    flt = message_filter(predicate, "Anything")

    if call == "filter, client, update":
        assert await flt(object(), object(), "the update") is True
    else:
        assert await type(flt).__call__(object(), "the update") is True

    assert seen == ["the update"], "only the update reaches the predicate"


def test_the_builder_names_the_filter_it_creates() -> None:
    """The name is what the library logs, and what a traceback would show without it."""

    async def predicate(update: Any) -> bool:
        return True

    assert type(message_filter(predicate, "NamedFilter")).__name__ == "NamedFilter"
