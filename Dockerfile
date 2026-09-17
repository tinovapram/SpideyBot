# ════════════════════════════════════════════════════════════════════
# Stage 1: Builder — compile wheels with build-time deps
# ════════════════════════════════════════════════════════════════════
FROM python:3.14-slim AS builder

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
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt

# ════════════════════════════════════════════════════════════════════
# Stage 2: Deno — copy binary only
# ════════════════════════════════════════════════════════════════════
FROM denoland/deno:bin AS deno

# ════════════════════════════════════════════════════════════════════
# Stage 3: Runtime — minimal image with non-root user
# NOTE: must match the builder's Python so installed packages are found.
# ════════════════════════════════════════════════════════════════════
FROM python:3.14-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    git \
    aria2 \
    mkvtoolnix \
    atomicparsley \
    procps \
    gosu \
    libnss3 \
    libatk-bridge2.0-0 \
    libdrm2 \
    libxkbcommon0 \
    libxcomposite1 \
    libxdamage1 \
    libxrandr2 \
    libgbm1 \
    libpango-1.0-0 \
    libasound2 \
    libatspi2.0-0 \
    libxshmfence1 \
    fonts-liberation \
    && rm -rf /var/lib/apt/lists/*

# Camoufox hard requirements. These package names are stable across Debian
# releases, so they are installed fail-fast — a silent miss here would only
# surface as a runtime browser launch failure.
#   xvfb          -> headless="virtual" display buffer
#   libgl1/egl    -> WebGL under Mesa software GLX
RUN apt-get update && apt-get install -y --no-install-recommends \
    xvfb \
    libx11-xcb1 \
    libxcb-shm0 \
    libxrender1 \
    libxfixes3 \
    libxi6 \
    libxext6 \
    libgl1 \
    libglx-mesa0 \
    libgl1-mesa-dri \
    libegl1 \
    && rm -rf /var/lib/apt/lists/*

# Remaining Firefox deps whose names vary across Debian releases (t64 renames),
# so install tolerantly rather than hard-failing the build.
RUN set -eux; \
    apt-get update; \
    for pkg in \
        libgtk-3-0 libgtk-3-0t64 \
        libdbus-glib-1-2 libdbus-glib-1-2t64 \
        libxt6 libxt6t64 \
        libxmu6 libxmu6t64 \
        libglib2.0-0 libglib2.0-0t64 \
        libfontconfig1 \
    ; do apt-get install -y --no-install-recommends "$pkg" || true; done; \
    rm -rf /var/lib/apt/lists/*

# Verify Xvfb really landed, so a broken virtual display fails the build
# instead of failing every /bypass at runtime. Xvfb also needs a writable
# socket dir, which slim images don't always ship.
RUN which Xvfb \
    && mkdir -p /tmp/.X11-unix \
    && chmod 1777 /tmp/.X11-unix

COPY --from=deno /deno /usr/local/bin/deno
COPY --from=builder /install /usr/local

# Camoufox install dir derives from XDG_CACHE_HOME (platformdirs), so pin it to
# a shared path both root (build time) and spideybot (runtime) can read.
ENV XDG_CACHE_HOME=/ms-camoufox
# Run Firefox on an Xvfb virtual display — stealthier than true headless.
ENV CAMOUFOX_HEADLESS=virtual

# Camoufox (stealth Firefox) — the /bypass browser engine
RUN camoufox fetch && chmod -R 777 /ms-camoufox

RUN groupadd -r spideybot && useradd -r -g spideybot -d /app -m spideybot

WORKDIR /app

COPY --chown=spideybot:spideybot . .

RUN mkdir -p data downloads user_sessions config/runtime config/cyberdrop-dl .gallery-dl \
    && chown -R spideybot:spideybot data downloads user_sessions config/runtime config/cyberdrop-dl .gallery-dl

# Strip BOM + CRLF from shell scripts (safety net for Windows builds)
RUN sed -i 's/\r$//' entrypoint.sh && sed -i '1s/^\xEF\xBB\xBF//' entrypoint.sh

COPY --chown=spideybot:spideybot entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh

# Health check: verify the bot process is alive
HEALTHCHECK --interval=60s --timeout=5s --start-period=15s --retries=3 \
    CMD pgrep -f "python main.py" > /dev/null || exit 1

CMD ["/app/entrypoint.sh"]
