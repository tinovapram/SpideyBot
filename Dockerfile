# syntax=docker/dockerfile:1

# Two stages exist for exactly one reason: `tgcrypto` (plan R1).
#
# It last released a wheel for cp311, so on Python 3.14 it must be compiled from a
# plain-C sdist. That compile needs a C toolchain, which the runtime image must not
# carry — a compiler in a production image is attack surface and ~300 MB of weight.
# So the builder produces wheels and the runtime installs them offline with
# `--no-index`, which also proves the runtime needs no compiler of its own.
#
# `tgcrypto` is not optional. Kurigram falls back to pure-Python AES if it is
# missing, and that fallback measured 0.59 MiB/s against 171.4 MiB/s — 291x slower
# (D1/D27). `spideybot check` fails loudly rather than letting that pass unnoticed.

# ── Stage 1: build wheels ────────────────────────────────────────────────
FROM python:3.14-slim AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

# build-essential is the whole point of this stage: gcc plus the libc headers that
# compiling a C extension needs. The official Python image already ships Python.h.
RUN apt-get update \
 && apt-get install -y --no-install-recommends build-essential \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /build
COPY requirements.txt ./
RUN pip wheel --wheel-dir /wheels --requirement requirements.txt

# ── Stage 2: runtime ─────────────────────────────────────────────────────
FROM python:3.14-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    SPIDEY_DATA_DIR=/data

# ffmpeg and ffprobe are required, not conveniences (D27): YouTube served 0 of 49
# single-file formats in testing, so without ffmpeg its downloads have no audio at
# all rather than merely a lower ceiling.
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg \
 && rm -rf /var/lib/apt/lists/*

COPY --from=builder /wheels /wheels
COPY requirements.txt ./
RUN pip install --no-index --find-links=/wheels --requirement requirements.txt \
 && rm -rf /wheels

# The session and cookie files are plaintext by design (D7-D9), so the service
# account's ownership of them is the only thing protecting them. Nothing runs as root.
RUN useradd --create-home --uid 10001 --shell /usr/sbin/nologin spideybot \
 && mkdir -p /data \
 && chown spideybot:spideybot /data

WORKDIR /app
COPY --chown=spideybot:spideybot . .
USER spideybot

VOLUME ["/data"]

# The capability report is the cheapest possible self-test, so it doubles as the
# image's healthcheck: it exits non-zero when a required capability is missing.
HEALTHCHECK --interval=5m --timeout=30s --start-period=15s \
    CMD ["python", "main.py", "check"]

CMD ["python", "main.py", "run"]
