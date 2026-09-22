# syntax=docker/dockerfile:1.7

# ════════════════════════════════════════════════════════════════════
# Build arguments
# ════════════════════════════════════════════════════════════════════

ARG UV_VERSION=0.12.17
ARG PYTHON_VERSION=3.14


# ════════════════════════════════════════════════════════════════════
# Stage 1: Builder
#
# Resolve Python dependencies into a relocatable virtual environment.
#
# The builder contains compilers and development headers that are
# required only when Python packages need to build native extensions.
#
# The runtime image never receives these build dependencies.
# ════════════════════════════════════════════════════════════════════

FROM ghcr.io/astral-sh/uv:${UV_VERSION}-python${PYTHON_VERSION}-trixie-slim AS builder

ENV DEBIAN_FRONTEND=noninteractive \
    UV_PYTHON_DOWNLOADS=0 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    VIRTUAL_ENV=/opt/venv \
    PATH="/opt/venv/bin:$PATH"

# ── Build dependencies ─────────────────────────────────────────────

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        build-essential \
        libcurl4-openssl-dev \
        libssl-dev \
        python3-dev \
        libjpeg62-turbo-dev \
        libpng-dev \
        libwebp-dev \
        zlib1g-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build

# ── Dependency files first ─────────────────────────────────────────
#
# This layer is cached as long as requirements.txt doesn't change.
# Application source changes will therefore NOT reinstall dependencies.

COPY requirements.txt .

# ── Create venv and install dependencies ───────────────────────────
#
# BuildKit cache keeps downloaded Python packages between builds.

RUN --mount=type=cache,target=/root/.cache/uv \
    uv venv /opt/venv \
        --python 3.14 \
        --python-preference only-system \
    && uv pip install \
        --python /opt/venv/bin/python \
        -r requirements.txt


# ════════════════════════════════════════════════════════════════════
# Stage 2: Camoufox
#
# Download the Camoufox browser using exactly the same Python
# environment created by the builder.
#
# This prevents the browser/package combination from drifting apart.
# ════════════════════════════════════════════════════════════════════

FROM builder AS camoufox

ENV XDG_CACHE_HOME=/root/.cache

RUN /opt/venv/bin/camoufox fetch


# ════════════════════════════════════════════════════════════════════
# Stage 3: Runtime
#
# Minimal runtime image.
#
# The container starts as root because entrypoint.sh needs to repair
# ownership of bind-mounted directories.
#
# The application itself ALWAYS runs as spideybot.
# ════════════════════════════════════════════════════════════════════

FROM ghcr.io/astral-sh/uv:${UV_VERSION}-python${PYTHON_VERSION}-trixie-slim AS runtime

ENV DEBIAN_FRONTEND=noninteractive

# ── Runtime system packages ────────────────────────────────────────
#
# No compilers or development headers here.
#
# procps provides pgrep for the Docker healthcheck.
# gosu is used by entrypoint.sh for the one-time privilege drop.

RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends \
        ffmpeg \
        git \
        aria2 \
        gosu \
        procps \
        fonts-liberation \
        libgtk-3-0 \
        libasound2 \
        libx11-xcb1 \
        libxcomposite1 \
        libxdamage1 \
        libxrandr2 \
        libgbm1 \
        libpango-1.0-0 \
        libcairo2 \
        libatk1.0-0 \
        libatk-bridge2.0-0 \
        libxshmfence1 \
        libdbus-glib-1-2 \
        libnss3 \
        xvfb; \
    rm -rf /var/lib/apt/lists/*; \
    which Xvfb; \
    which gosu; \
    which pgrep; \
    mkdir -p /tmp/.X11-unix; \
    chmod 1777 /tmp/.X11-unix


# ════════════════════════════════════════════════════════════════════
# Application user
# ════════════════════════════════════════════════════════════════════

RUN groupadd --system spideybot \
    && useradd \
        --system \
        --gid spideybot \
        --home-dir /app \
        --create-home \
        spideybot


# ════════════════════════════════════════════════════════════════════
# Python virtual environment
# ════════════════════════════════════════════════════════════════════

COPY --from=builder /opt/venv /opt/venv

ENV VIRTUAL_ENV=/opt/venv \
    PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_PYTHON_DOWNLOADS=0


# ════════════════════════════════════════════════════════════════════
# Camoufox browser
# ════════════════════════════════════════════════════════════════════
#
# The browser was fetched in the camoufox stage using the same venv.
#
# Store it under the application user's home instead of /root and
# don't use chmod 777.
# ════════════════════════════════════════════════════════════════════

ENV XDG_CACHE_HOME=/home/spideybot/.cache \
    CAMOUFOX_HEADLESS=virtual

COPY --from=camoufox \
    /root/.cache/camoufox \
    /home/spideybot/.cache/camoufox

RUN chown -R spideybot:spideybot \
    /home/spideybot/.cache


# ════════════════════════════════════════════════════════════════════
# Application
# ════════════════════════════════════════════════════════════════════

WORKDIR /app

# Application code is copied LAST.
#
# Changes to application source therefore don't invalidate:
#   - apt layer
#   - Python environment
#   - Camoufox browser

COPY --chown=spideybot:spideybot . .


# ════════════════════════════════════════════════════════════════════
# Runtime directories
# ════════════════════════════════════════════════════════════════════

RUN mkdir -p \
        data \
        downloads \
        user_sessions \
        config/runtime \
        config/cyberdrop-dl \
        .gallery-dl \
    && chown -R spideybot:spideybot \
        data \
        downloads \
        user_sessions \
        config/runtime \
        config/cyberdrop-dl \
        .gallery-dl \
    && sed -i 's/\r$//' entrypoint.sh \
    && sed -i '1s/^\xEF\xBB\xBF//' entrypoint.sh \
    && chmod 0755 entrypoint.sh


# ════════════════════════════════════════════════════════════════════
# Security
# ════════════════════════════════════════════════════════════════════
#
# Keep root here because entrypoint.sh needs root ONLY during startup
# to fix ownership of Docker bind mounts.
#
# entrypoint.sh immediately performs:
#
#     exec gosu spideybot "$0" "$@"
#
# After that, everything runs as spideybot.
# ════════════════════════════════════════════════════════════════════

USER root


# ════════════════════════════════════════════════════════════════════
# Healthcheck
# ════════════════════════════════════════════════════════════════════

HEALTHCHECK \
    --interval=60s \
    --timeout=5s \
    --start-period=20s \
    --retries=3 \
    CMD pgrep -f "python.*main.py" > /dev/null || exit 1


# ════════════════════════════════════════════════════════════════════
# Entrypoint
# ════════════════════════════════════════════════════════════════════

ENTRYPOINT ["/app/entrypoint.sh"]