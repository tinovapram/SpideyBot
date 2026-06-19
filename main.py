"""
SpideyBot — Root entry point.

Thin wrapper that delegates to the spideybot package.
Replaces the old bot.py as the main entry point.
"""

from spideybot.bot import main

if __name__ == '__main__':
    main()
