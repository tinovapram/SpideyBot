#!/bin/bash
set -Eeuo pipefail

APP_USER="spideybot"
APP_GROUP="spideybot"
APP_DIR="/app"

# Directories that may be bind-mounted from the host.
RUNTIME_DIRS=(
    "${APP_DIR}/data"
    "${APP_DIR}/downloads"
    "${APP_DIR}/user_sessions"
    "${APP_DIR}/config/runtime"
    "${APP_DIR}/.gallery-dl"
)


# ════════════════════════════════════════════════════════════════════
# Root-only initialization
# ════════════════════════════════════════════════════════════════════

if [[ "$(id -u)" -eq 0 ]]; then

    echo "[entrypoint] Running root initialization..."

    # Bind mounts may not exist inside the image or may have been
    # created by Docker as root.
    mkdir -p "${RUNTIME_DIRS[@]}"

    # Make the runtime directories writable by the application user.
    chown -R "${APP_USER}:${APP_GROUP}" "${RUNTIME_DIRS[@]}"

    echo "[entrypoint] Dropping privileges to ${APP_USER}..."

    # Re-execute this same script as spideybot.
    #
    # From this point onward, no commands run as root.
    exec gosu "${APP_USER}" "$@"
fi


# ════════════════════════════════════════════════════════════════════
# Everything below runs as spideybot
# ════════════════════════════════════════════════════════════════════

cd "${APP_DIR}"

echo "[entrypoint] Running database migrations..."

/opt/venv/bin/alembic upgrade head

echo "[entrypoint] Starting SpideyBot..."

# Replace the shell with Python.
#
# This makes Python the main application process and allows Docker
# signals such as SIGTERM to reach it directly.
exec /opt/venv/bin/python main.py