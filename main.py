"""SpideyBot entry point.

    python main.py db init|start|stop|status|migrate|reset
    python main.py run

``app/cli.py`` holds the wiring; this file exists so the commands documented in
the plan and README are the ones an operator actually types.
"""

from __future__ import annotations

from app.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
