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
# Stage 3: Runtime — minimal image with non-root user
# NOTE: same image tag as the builder so the venv's interpreter path (and
# therefore every console script shebang) stays valid.
#
# Layer cache strategy: things that change rarely (apt deps, bun binary,
# venv, camoufox) come first; application code is last so rebuilds skip
# the expensive layers above.
# ════════════════════════════════════════════════════════════════════
FROM ghcr.io/astral-sh/uv:0.12.17-python3.14-trixie-slim

# ── System packages: single apt layer to avoid 3× apt-get update ──
# Core runtime tools + Camoufox hard requirements (stable names) +
# Firefox/t64 variant deps (tolerant fallback). Xvfb + socket dir
# validated here so a broken display fails the build, not every /bypass.
RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends \
        ffmpeg git aria2 mkvtoolnix atomicparsley procps gosu \
        fonts-liberation \
        libnss3 libatk-bridge2.0-0 libdrm2 libxkbcommon0 \
        libxcomposite1 libxdamage1 libxrandr2 libgbm1 \
        libpango-1.0-0 libasound2 libatspi2.0-0 libxshmfence1 \
        xvfb libx11-xcb1 libxcb-shm0 libxrender1 libxfixes3 \
        libxi6 libxext6 libgl1 libglx-mesa0 libgl1-mesa-dri libegl1; \
    for pkg in \
        libgtk-3-0 libgtk-3-0t64 \
        libdbus-glib-1-2 libdbus-glib-1-2t64 \
        libxt6 libxt6t64 \
        libxmu6 libxmu6t64 \
        libglib2.0-0 libglib2.0-0t64 \
        libfontconfig1 \
    ; do apt-get install -y --no-install-recommends "$pkg" || true; done; \
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

# ── Camoufox (stealth Firefox) — the /bypass browser engine ──
# XDG_CACHE_HOME pins the install to a shared root/spideybot path.
ENV XDG_CACHE_HOME=/ms-camoufox \
    CAMOUFOX_HEADLESS=virtual
RUN camoufox fetch && chmod -R 777 /ms-camoufox

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
