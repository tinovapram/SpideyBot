# ════════════════════════════════════════════════════════════════════
# Stage 1: Builder – compile Python wheels with build-time deps
# ════════════════════════════════════════════════════════════════════
FROM python:3.11-slim AS builder

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
# Stage 2: Deno – copy binary only
# ════════════════════════════════════════════════════════════════════
FROM denoland/deno:bin AS deno

# ════════════════════════════════════════════════════════════════════
# Stage 3: Runtime – minimal image with non-root user
# ════════════════════════════════════════════════════════════════════
FROM python:3.11-slim

# Runtime-only system deps (no compilers, no -dev headers)
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    git \
    mkvtoolnix \
    atomicparsley \
    gosu \
    && rm -rf /var/lib/apt/lists/*

# Copy Deno binary from build stage
COPY --from=deno /deno /usr/local/bin/deno

# Copy pre-built Python packages from builder stage
COPY --from=builder /install /usr/local

# ── Non-root user ───────────────────────────────────────────────
RUN groupadd -r spideybot && useradd -r -g spideybot -d /app -m spideybot

WORKDIR /app

# Copy application code (owned by spideybot)
COPY --chown=spideybot:spideybot . .

# Ensure runtime directories exist with correct ownership
RUN mkdir -p data downloads user_sessions config/runtime .gallery-dl \
    && chown -R spideybot:spideybot data downloads user_sessions config/runtime .gallery-dl

# Health check: verify Python process is alive
HEALTHCHECK --interval=60s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import telethon" || exit 1

COPY --chown=spideybot:spideybot entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh

# Entrypoint runs as root to fix bind-mount permissions, then drops to spideybot via gosu
CMD ["/app/entrypoint.sh"]
