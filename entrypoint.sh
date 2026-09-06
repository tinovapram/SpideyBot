#!/bin/bash
set -e

# Fix permissions on bind-mounted directories (runs as root)
mkdir -p /app/data /app/downloads /app/user_sessions /app/config/runtime /app/.gallery-dl
chown -R spideybot:spideybot /app/data /app/downloads /app/user_sessions /app/config/runtime /app/.gallery-dl

# Run database migrations (non-root)
gosu spideybot alembic upgrade head

# Start the bot (replaces shell)
exec gosu spideybot python main.py
