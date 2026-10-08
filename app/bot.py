"""Bot assembly and lifecycle.

The order inside :func:`run` is the whole point of the module: uvloop has to be
installed before any event loop exists, and logging before anything can log —
including the client, which logs while it is being constructed.
"""

from __future__ import annotations

from app import config, paths
from app import logging as app_logging
from telegram import clients


def _install_uvloop() -> bool:
    """Use uvloop if it is available.

    Unix-only, so on Windows this reports "not installed" rather than failing: the
    bot is developed on Windows and deployed on Linux.
    """
    try:
        import uvloop
    except ImportError:
        return False

    uvloop.install()
    return True


def run() -> int:
    config.load_env_file()
    settings = config.get_settings()
    paths.ensure_runtime_dirs()

    uvloop_used = _install_uvloop()
    redactor = app_logging.setup_logging(secrets=settings.secret_values())
    log = app_logging.get_logger("spideybot")

    log.info(
        "starting",
        uvloop=uvloop_used,
        client_profile=settings.client.profile,
        test_mode=settings.test_mode,
        redacted_values=redactor.count,
    )

    for directory in paths.insecure_secret_dirs():
        log.warning("credential directory is readable beyond this account", path=str(directory))

    if settings.test_mode:
        log.warning("test mode: using Telegram's test servers and data/sessions/test")

    client = clients.build_bot(settings)
    try:
        client.run()
    except KeyboardInterrupt:
        log.info("interrupted")
    finally:
        log.info("stopped")

    return 0
