"""Secret lookup, behind a seam.

Plan D12: secrets reach the app through one narrow interface, so moving from the
environment to an OS keyring or Docker secrets later is an addition rather than a
rewrite. ``EnvSecretsProvider`` is the only implementation today, and it is also
what makes the config layer testable — tests inject a dict-backed provider.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Protocol, runtime_checkable


@runtime_checkable
class SecretsProvider(Protocol):
    def get(self, name: str) -> str | None:
        """Return the secret for ``name``, or ``None`` when it is not set."""
        ...


class EnvSecretsProvider:
    """Reads secrets from the process environment.

    ``.env`` is loaded into the environment at startup, so this covers both a
    real environment and a local ``.env`` file with no second code path.
    """

    def __init__(self, env: Mapping[str, str] | None = None) -> None:
        self._env = os.environ if env is None else env

    def get(self, name: str) -> str | None:
        # An empty value means "not set": the shipped .env.example declares every
        # key present-but-blank, so blank must not read as a real value.
        return self._env.get(name) or None


class DictSecretsProvider:
    """In-memory provider, for tests."""

    def __init__(self, values: Mapping[str, str]) -> None:
        self._values = {key: value for key, value in values.items() if value}

    def get(self, name: str) -> str | None:
        return self._values.get(name)
