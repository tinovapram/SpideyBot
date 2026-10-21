"""Structured logging configuration (structlog)."""

from __future__ import annotations

import logging
import re
import sys

import structlog

_SECRET_KEY_RE = re.compile(r"(?:cookie|token|password|api[_-]?hash|secret|authorization|session)", re.I)
_QUERY_SECRET_RE = re.compile(
    r"([?&](?:token|access_token|auth|cookie|session|sig|signature|key|password)=)[^&\s]+",
    re.I,
)


def _redact(value: object, *, key: str = "") -> object:
    if _SECRET_KEY_RE.search(key):
        return "[REDACTED]"
    if isinstance(value, str):
        return _QUERY_SECRET_RE.sub(r"\1[REDACTED]", value)
    return value


def redact_sensitive_fields(
    _logger: object,
    _method_name: str,
    event_dict: dict[str, object],
) -> dict[str, object]:
    """Redact secret-like fields and sensitive URL query parameters."""
    return {key: _redact(value, key=key) for key, value in event_dict.items()}


def setup_logging(log_level: str = "INFO") -> None:
    """Configure structlog + stdlib logging once at startup."""
    for handler in logging.root.handlers[:]:
        logging.root.removeHandler(handler)

    logging.basicConfig(
        format="%(message)s",
        level=getattr(logging, log_level.upper(), logging.INFO),
        stream=sys.stdout,
    )

    for noisy in ("httpx", "httpcore", "urllib3", "aiohttp", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    shared_processors: list[structlog.types.Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.stdlib.PositionalArgumentsFormatter(),
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.UnicodeDecoder(),
        redact_sensitive_fields,
    ]

    structlog.configure(
        processors=[
            *shared_processors,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )
