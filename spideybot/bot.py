"""
SpideyBot - Backward Compatibility Redirect.

The bot logic has moved to spideybot.core.bot.
This file is kept for backward compatibility (main.py, __main__.py).
"""

from spideybot.core.bot import main, bot, download_queue_manager

__all__ = ["main", "bot", "download_queue_manager"]
