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

import importlib
import shutil
import sys
from dataclasses import dataclass
from types import ModuleType

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
            "Install the prebuilt accelerator: 'uv pip install tgcrypto-pyrofork'",
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


def fast_event_loop() -> ModuleType | None:
    """The module that installs the fastest event loop this platform offers.

    ``uvloop`` has no Windows build, so Windows is served by ``winuvloop``, which selects
    ``winloop`` there and delegates to ``uvloop`` elsewhere. ``None`` means neither is
    installed, which is a supported configuration rather than a fault.
    """
    for module_name in ("winuvloop", "uvloop"):
        try:
            return importlib.import_module(module_name)
        except ImportError:
            continue
    return None


def check_uvloop() -> Capability:
    """Optional: the fast event loop. Absent is a platform fact, never a fault."""
    module = fast_event_loop()
    if module is None:
        return Capability(
            "uvloop",
            False,
            "not installed; the bot uses the standard event loop",
            required=False,
        )

    # winuvloop reports which backend it picked; uvloop is the backend itself.
    if hasattr(module, "backend_name"):
        backend = f"{module.backend_name()} {module.backend_version()}"
        detail = f"available via {module.__name__} ({backend})"
    else:
        detail = f"available ({module.__name__} {module.__version__})"
    return Capability("uvloop", True, detail, required=False)


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
