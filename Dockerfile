# ════════════════════════════════════════════════════════════════════
# Stage 1: Builder — resolve deps with uv into a relocatable venv
#
# Base image is the official python:3.14-slim build with the uv binary layered
# on top (Debian trixie, CPython 3.14.7 at /usr/local/bin/python), so there is
# no separate uv-copy stage and builder/runtime cannot drift apart.
# ════════════════════════════════════════════════════════════════════
FROM ghcr.io/astral-sh/uv:0.12.17-python3.14-trixie-slim AS builder

# Byte-compile on install and copy (not hardlink) so the venv survives the copy
# into the runtime stage. UV_PYTHON_DOWNLOADS=0 keeps uv on the interpreter the
# base image already ships instead of fetching a managed one.
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=0

ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
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
COPY requirements.txt .
RUN uv venv /opt/venv --python 3.14 --python-preference only-system \
    && uv pip install --no-cache --python /opt/venv/bin/python -r requirements.txt

# ════════════════════════════════════════════════════════════════════
# Stage 2: Bun — copy binary only
# ════════════════════════════════════════════════════════════════════
FROM oven/bun:1 AS bun

# ════════════════════════════════════════════════════════════════════
# Stage 3: Camoufox — pre-fetch browser binaries (~300 MB).
# This stage is ONLY rebuilt when CAMOUFOX_VER changes, so venv or
# code rebuilds never re-download the browser.
# ════════════════════════════════════════════════════════════════════
FROM python:3.14-slim AS camoufox-bin
ARG CAMOUFOX_VER=0.5.6
RUN pip install --no-cache-dir "camoufox>=${CAMOUFOX_VER}" \
    && camoufox fetch

# ════════════════════════════════════════════════════════════════════
# Stage 4: Runtime — minimal image with non-root user
#
# Layer cache strategy: things that change rarely (apt deps, bun binary,
# venv, camoufox) come first; application code is last so rebuilds skip
# the expensive layers above.
# ════════════════════════════════════════════════════════════════════
FROM ghcr.io/astral-sh/uv:0.12.17-python3.14-trixie-slim

# ── System packages (matches upstream camoufox-docker + our tool needs) ──
# Only packages Camoufox actually requires + the tools we call.
# ponytail: recheck when upgrading Camoufox; browser deps are upstream's call.
ENV DEBIAN_FRONTEND=noninteractive
RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends \
        ffmpeg git aria2 gosu \
        fonts-liberation \
        libgtk-3-0 libasound2 libx11-xcb1 libxcomposite1 \
        libxdamage1 libxrandr2 libgbm1 libpango-1.0-0 \
        libcairo2 libatk1.0-0 libatk-bridge2.0-0 libxshmfence1 \
        libdbus-glib-1-2 libnss3 \
        xvfb; \
    rm -rf /var/lib/apt/lists/*; \
    which Xvfb; \
    mkdir -p /tmp/.X11-unix && chmod 1777 /tmp/.X11-unix

# ── Bun (single binary, no package manager) ──
COPY --from=bun /usr/local/bin/bun /usr/local/bin/bunx

# ── Python venv from builder ──
COPY --from=builder /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH" \
    VIRTUAL_ENV=/opt/venv \
    UV_PYTHON_DOWNLOADS=0

# ── Camoufox (stealth Firefox) — pre-fetched browser binaries ──
# COPY from the dedicated stage instead of running `camoufox fetch` here.
# Only rebuilds when CAMOUFOX_VER changes (bump to update browser).
ENV XDG_CACHE_HOME=/ms-camoufox \
    CAMOUFOX_HEADLESS=virtual
COPY --from=camoufox-bin /root/.cache/camoufox /ms-camoufox/camoufox
RUN chmod -R 777 /ms-camoufox

# ── Non-root user ──
RUN groupadd -r spideybot && useradd -r -g spideybot -d /app -m spideybot

# ── Application code (changes every rebuild — always last) ──
WORKDIR /app
COPY --chown=spideybot:spideybot . .

# Bind-mount dirs, sandbox dirs, BOM/CRLF fix, entrypoint perms — one layer.
RUN mkdir -p data downloads user_sessions config/runtime config/cyberdrop-dl .gallery-dl \
    && chown -R spideybot:spideybot data downloads user_sessions config/runtime config/cyberdrop-dl .gallery-dl \
    && sed -i 's/\r$//' entrypoint.sh \
    && sed -i '1s/^\xEF\xBB\xBF//' entrypoint.sh \
    && chmod +x entrypoint.sh

# Health check: verify the bot process is alive
HEALTHCHECK --interval=60s --timeout=5s --start-period=15s --retries=3 \
    CMD pgrep -f "python main.py" > /dev/null || exit 1

CMD ["/app/entrypoint.sh"]
