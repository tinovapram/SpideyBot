"""Fast, hermetic tests: no database, no network, no Telegram.

A package (rather than a plain directory) so that two layers may hold a file with the same
basename — ``tests/integration/test_quota.py`` and ``tests/acceptance/test_quota.py`` — without
pytest failing collection with an "import file mismatch". Without this, every new layer file
would have to invent a globally unique name.
"""
