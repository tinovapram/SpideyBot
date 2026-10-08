"""Runtime capability checks (plan D27).

Two dependencies decide whether the bot is *usable* rather than merely importable, and
neither announces its absence usefully on its own:

- ``tgcrypto`` — Kurigram silently falls back to pure-Python AES, which was measured at
  0.59 MiB/s against 171.4 MiB/s, so its own log line ("a much slower speed") understates
  the problem by orders of magnitude.
- ``ffmpeg`` — YouTube serves no single-file formats at all (0 of 49 in a sample), so
  without it the widest source in the spec produces nothing deliverable.

Both are therefore **required** capabilities: reported at startup, re-checked on demand by
``spideybot check``, and consulted by code that needs them so a job fails with the reason
instead of degrading quietly.
"""

from __future__ import annotations

import shutil
import sys
from dataclasses import dataclass

# Measured on this project's development machine; see plan section 0.
FAST_AES_SPEEDUP = 291
FFMPEG_HINT = (
    "Install it and put it on PATH: 'apt-get install -y ffmpeg' (Debian/Ubuntu) or "
    "'winget install Gyan.FFmpeg' (Windows)"
)


@dataclass(frozen=True)
class Capability:
    name: str
    ok: bool
    detail: str
    required: bool = True

    @property
    def status(self) -> str:
        if self.ok:
            return "ok"
        return "MISSING" if self.required else "absent"


def check_tgcrypto() -> Capability:
    """Whether Kurigram will really use the C AES accelerator.

    Importing ``tgcrypto`` is not sufficient evidence on its own: what matters is which
    branch of ``pyrogram.crypto.aes`` ran, and the fallback is recognisable because it
    defines ``ige`` and ``ctr`` and imports ``pyaes``.
    """
    from pyrogram.crypto import aes

    used_fallback = hasattr(aes, "pyaes") or hasattr(aes, "ige")
    if used_fallback:
        return Capability(
            "tgcrypto",
            False,
            "Kurigram is using pure-Python AES, which measures "
            f"~{FAST_AES_SPEEDUP}x slower (0.59 vs 171.4 MiB/s). "
            "It has no wheel above cp311, so it must be compiled: 'uv pip install tgcrypto'",
        )
    return Capability("tgcrypto", True, "Kurigram is using the C AES accelerator")


def check_ffmpeg() -> Capability:
    """Whether ffmpeg is reachable.

    Resolved through PATH rather than a fixed location: the Windows winget install lives
    under a version-stamped directory that an upgrade would move.
    """
    path = shutil.which("ffmpeg")
    if path is None:
        return Capability("ffmpeg", False, f"not found on PATH. {FFMPEG_HINT}")
    return Capability("ffmpeg", True, path)


def check_ffprobe() -> Capability:
    """ffprobe ships with ffmpeg and is used to read media metadata."""
    path = shutil.which("ffprobe")
    return Capability("ffprobe", path is not None, path or f"not found on PATH. {FFMPEG_HINT}")


def check_uvloop() -> Capability:
    """Optional: a Unix-only event loop. Its absence on Windows is expected, not a fault."""
    try:
        import uvloop
    except ImportError:
        return Capability(
            "uvloop",
            False,
            "not installed (Windows has no uvloop); the bot uses the standard event loop",
            required=False,
        )
    return Capability("uvloop", True, f"available ({uvloop.__version__})", required=False)


def probe() -> list[Capability]:
    """Every capability, in report order: required ones first."""
    return [
        Capability("python", True, sys.version.split()[0], required=False),
        check_tgcrypto(),
        check_ffmpeg(),
        check_ffprobe(),
        check_uvloop(),
    ]


def missing_required(capabilities: list[Capability]) -> list[Capability]:
    return [capability for capability in capabilities if capability.required and not capability.ok]
