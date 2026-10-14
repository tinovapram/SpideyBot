# syntax=docker/dockerfile:1

# Two stages: the builder materialises a wheelhouse, and the runtime installs from it
# offline with `--no-index`, which proves the runtime needs no compiler of its own — a
# compiler in a production image is attack surface and ~300 MB of weight.
#
# Compiling the AES accelerator from source is what forced this split in the first place
# (plan R1): upstream `tgcrypto` last released a wheel for cp311. The accelerator now
# arrives as `tgcrypto-pyrofork`, which ships a cp314 manylinux wheel, so build-essential
# below is a safety net for any dependency that still offers only an sdist rather than
# something the accelerator itself needs.
#
# The accelerator is not optional. Kurigram falls back to pure-Python AES if no
# `tgcrypto` module imports, and that fallback measured 0.59 MiB/s against 171.4 MiB/s
# — 291x slower (D1/D27). `spideybot check` fails loudly rather than letting that pass.

# ── Stage 1: build wheels ────────────────────────────────────────────────
FROM python:3.14-slim AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

# gcc plus the libc headers that compiling a C extension needs — the official Python image
# already ships Python.h. This stage is discarded, so the toolchain costs the shipped image
# nothing; it stays as the fallback for any dependency that ships only an sdist, now that
# the accelerator no longer needs it.
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

COPY requirements.txt ./
# The wheels are mounted rather than copied. A `COPY --from=builder` creates its own
# layer, and no later cleanup can remove the wheels from it — they would ship inside
# the image despite pip having already installed them. Measured at 77 MB. A BuildKit
# mount leaves nothing behind.
RUN --mount=from=builder,source=/wheels,target=/wheels \
    pip install --no-index --find-links=/wheels --requirement requirements.txt

# The session and cookie files are plaintext by design (D7-D9), so the service
# account's ownership of them is the only thing protecting them. Nothing runs as root.
RUN useradd --create-home --uid 10001 --shell /usr/sbin/nologin spideybot \
 && mkdir -p /data \
 && chown spideybot:spideybot /data

# postgresql-binaries unpacks its server on first use, into site-packages — which the service
# account cannot write, so `db init` would fail with a permission error on the directory pip
# owns. Unpacking it here, where root can, is what makes the bundled provider a real choice
# inside the container rather than one that only works from a writable checkout.
RUN SITE=$(python -c "import site; print(site.getsitepackages()[0])") \
 && python -c "import postgresql_binaries; postgresql_binaries.bin()" \
 && chown -R spideybot:spideybot "$SITE/postgresql_binaries"

WORKDIR /app
COPY --chown=spideybot:spideybot . .
USER spideybot

VOLUME ["/data"]

# The capability report is the cheapest possible self-test, so it doubles as the
# image's healthcheck: it exits non-zero when a required capability is missing.
HEALTHCHECK --interval=5m --timeout=30s --start-period=15s \
    CMD ["python", "main.py", "check"]

CMD ["python", "main.py", "run"]
