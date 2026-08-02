#!/bin/bash
set -e

# Fix permissions on bind-mounted directories (runs as root)
mkdir -p /app/data /app/downloads /app/user_sessions /app/config/runtime /app/.gallery-dl
chown -R spideybot:spideybot /app/data /app/downloads /app/user_sessions /app/config/runtime /app/.gallery-dl

# Drop to spideybot user and run the bot
exec gosu spideybot python main.py
