"""Shared test setup.

``SPIDEY_DATABASE_URL`` is required by the application (no credential-bearing
default), so the suite supplies a local placeholder when the environment does
not define one.  Tests that need a real connection use
``SPIDEY_TEST_DATABASE_URL`` instead.
"""

from __future__ import annotations

import os

os.environ.setdefault(
    "SPIDEY_DATABASE_URL",
    "postgresql+asyncpg://spidey:spidey@localhost:5432/spideybot",
)
