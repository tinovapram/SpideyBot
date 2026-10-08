"""Structured logging that cannot leak a secret (NFR-6).

Redaction is by *value*: every secret the settings know about is replaced wherever
it appears, including inside exception text. That matters more than usual here,
because session files and cookies are stored in plaintext by design (D7-D9) — so a
log line is the most likely way one escapes.

:func:`mask_secret` is the *display* helper, not a security boundary: it is what
the Telegram UI shows for a stored cookie.
"""

from __future__ import annotations

import logging
import sys
from collections.abc import Iterable
from typing import Any

import structlog

REDACTED = "***REDACTED***"

# Below this, replacement does more harm than good: a 3-character "secret" would
# redact unrelated text everywhere it happened to appear.
_MIN_SECRET_LENGTH = 6


class Redactor:
    """Replaces registered secret values wherever they appear."""

    def __init__(self, values: Iterable[str] = ()) -> None:
        self._values: list[str] = []
        self.register_many(values)

    def register(self, value: str | None) -> None:
        if not value or len(value) < _MIN_SECRET_LENGTH:
            return
        if value in self._values:
            return
        self._values.append(value)
        # Longest first, so a joined value is replaced before its own parts and
        # cannot be half-redacted.
        self._values.sort(key=len, reverse=True)

    def register_many(self, values: Iterable[str]) -> None:
        for value in values:
            self.register(value)

    @property
    def count(self) -> int:
        """How many values are registered. Exposed so tests can assert on it."""
        return len(self._values)

    def scrub(self, text: str) -> str:
        for value in self._values:
            text = text.replace(value, REDACTED)
        return text

    def scrub_value(self, value: Any) -> Any:
        if isinstance(value, str):
            return self.scrub(value)
        if isinstance(value, dict):
            return {key: self.scrub_value(item) for key, item in value.items()}
        if isinstance(value, (list, tuple, set)):
            return type(value)(self.scrub_value(item) for item in value)
        return value


class RedactingFilter(logging.Filter):
    """Last line of defence for the stdlib path.

    structlog renders through ``logging``, and tracebacks are formatted by stdlib
    *after* the processors run, so this filter is what catches a secret that only
    shows up inside an exception message.
    """

    def __init__(self, redactor: Redactor) -> None:
        super().__init__()
        self._redactor = redactor

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = self._redactor.scrub(record.msg)
        if record.args:
            # A lone mapping arrives as the mapping itself (Python 3.14) and is what
            # `getMessage` formats through, so it has to be scrubbed in place of the
            # usual positional tuple.
            if isinstance(record.args, dict):
                record.args = self._redactor.scrub_value(record.args)
            else:
                record.args = tuple(self._redactor.scrub_value(arg) for arg in record.args)
        if record.exc_text:
            record.exc_text = self._redactor.scrub(record.exc_text)
        return True


def _redaction_processor(redactor: Redactor) -> Any:
    def processor(logger: Any, method_name: str, event_dict: dict[str, Any]) -> dict[str, Any]:
        return redactor.scrub_value(event_dict)

    return processor


def mask_secret(value: str, *, keep_first: int = 4, keep_last: int = 2) -> str:
    """Display form for a stored credential.

    The spec asks for the first 4 and last 2 characters of an ``ndus`` value; a
    value too short to hide anything behind those windows is masked entirely
    rather than partly revealed.
    """
    if not value:
        return ""
    if len(value) <= keep_first + keep_last:
        return "*" * len(value)
    hidden = len(value) - keep_first - keep_last
    return f"{value[:keep_first]}{'*' * hidden}{value[-keep_last:]}"


def setup_logging(
    *,
    level: int = logging.INFO,
    secrets: Iterable[str] = (),
    json_output: bool = False,
) -> Redactor:
    """Configure structlog and stdlib logging; returns the redactor in force.

    Nothing is logged until this runs, so the entry points call it as soon as
    settings are available — and before any client is constructed.
    """
    redactor = Redactor(secrets)
    renderer = (
        structlog.processors.JSONRenderer()
        if json_output
        else structlog.dev.ConsoleRenderer(colors=False)
    )

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.StackInfoRenderer(),
            # Exc_info must be stringified before redaction, otherwise the traceback
            # is still a live object and cannot be scrubbed.
            structlog.processors.format_exc_info,
            _redaction_processor(redactor),
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        cache_logger_on_first_use=True,
    )

    handler = logging.StreamHandler(sys.stdout)
    handler.addFilter(RedactingFilter(redactor))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)

    return redactor


def get_logger(name: str | None = None) -> Any:
    return structlog.get_logger(name)
