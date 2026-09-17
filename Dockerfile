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

COPY --from=deno /deno /usr/local/bin/deno
COPY --from=builder /install /usr/local

# Playwright Chromium (for /bypass link-shortener resolution)
RUN playwright install chromium

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
