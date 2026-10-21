from __future__ import annotations

import sys
from types import SimpleNamespace

from core.event_loop import configure_event_loop


def test_configure_event_loop_installs_winuvloop(monkeypatch) -> None:
    installed: list[bool] = []
    fake_winuvloop = SimpleNamespace(
        install=lambda: installed.append(True),
        backend_name=lambda: "test-backend",
    )
    monkeypatch.setitem(sys.modules, "winuvloop", fake_winuvloop)

    assert configure_event_loop() == "test-backend"
    assert installed == [True]
