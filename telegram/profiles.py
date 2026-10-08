"""Client identity profiles (plan D16).

The strings here are what a user sees in their own *Active Sessions* list, so this is
an account-safety setting, not cosmetics: a client that looks like an official
desktop app is less likely to be actioned than one that announces itself as a
library. The presets come from the Kurigram docs and are kept verbatim so they can
be diffed against upstream.

Kurigram's parameter is ``client_platform`` (not ``platform``, as the docs' sample
tables call it), and it takes an enum — so an unrecognised value is dropped rather
than passed through, which would raise at startup over a cosmetic field.
"""

from __future__ import annotations

from typing import Any

from pyrogram import enums

from app.config import ClientProfile


def _client_platform(value: str) -> Any | None:
    """Enum member for a platform name, or ``None`` when it is not one."""
    enum_cls = getattr(enums, "ClientPlatform", None)
    if enum_cls is None:
        return None
    try:
        return enum_cls[value.strip().upper()]
    except KeyError:
        return None


def identity_kwargs(profile: ClientProfile) -> dict[str, Any]:
    """The Kurigram ``Client`` kwargs that decide how this session appears."""
    kwargs: dict[str, Any] = {
        "device_model": profile.device_model,
        "system_version": profile.system_version,
        "app_version": profile.app_version,
        "lang_code": profile.lang_code,
        "lang_pack": profile.lang_pack,
    }
    platform = _client_platform(profile.platform)
    if platform is not None:
        kwargs["client_platform"] = platform
    return kwargs
