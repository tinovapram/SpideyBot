"""Command handlers.

Kurigram's smart-plugin loader imports every module in this package and collects the
handlers they declare, so each command lives in its own module and is registered by
the ``@Client.on_message`` decorator at import time. ``__init__.py`` itself is
skipped by the loader and is intentionally empty.
"""
