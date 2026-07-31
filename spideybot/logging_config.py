"""
SpideyBot — Structured Logging Configuration.

Sets up structlog with console-friendly rendering in development
and JSON output in production. Binds common context (service name)
globally so every log line carries it automatically.
"""

import logging
import sys

import structlog


def setup_logging(log_level: str = "INFO") -> None:
    """Configure structlog + stdlib logging for the application.

    Call once at process startup (before any ``get_logger()`` calls).

    Args:
        log_level: Global log level string (DEBUG, INFO, WARNING, ERROR, CRITICAL).
    """
    # Reset any prior basicConfig so we don't get duplicate handlers
    for handler in logging.root.handlers[:]:
        logging.root.removeHandler(handler)

    # Configure stdlib logging — structlog processors will run inside this
    logging.basicConfig(
        format="%(message)s",
        level=getattr(logging, log_level.upper(), logging.INFO),
        stream=sys.stdout,
    )

    # Silence overly chatty third-party loggers
    for noisy in ("httpx", "httpcore", "urllib3", "aiohttp", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    # Shared processors applied to every log event (before serializer)
    shared_processors: list[structlog.types.Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.stdlib.PositionalArgumentsFormatter(),
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.UnicodeDecoder(),
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
