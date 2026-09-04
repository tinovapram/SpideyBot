"""
User account session manager.

Sessions are stored as Telethon **file sessions** (SQLite) under
``user_sessions/<user_id>.session``. For backward compatibility, legacy
encrypted StringSession files (``user_sessions/<user_id>.json``) are detected
and migrated to the new file format automatically on first use.
"""

from __future__ import annotations

import json
from pathlib import Path

import structlog
from cryptography.fernet import Fernet
from telethon import TelegramClient
from telethon.sessions import SQLiteSession, StringSession

from core import config
from utils import paths

logger = structlog.get_logger(__name__)

_active_clients: dict[int, TelegramClient] = {}
_fernet: Fernet | None = None


# ── Helpers ───────────────────────────────────────────────────────

def session_file(user_id: int) -> Path:
    """Path of the Telethon file session for *user_id*."""
    return paths.SESSIONS_DIR / f"{user_id}.session"


def legacy_file(user_id: int) -> Path:
    """Path of a legacy encrypted StringSession JSON file."""
    return paths.SESSIONS_DIR / f"{user_id}.json"


def _fernet_cipher() -> Fernet:
    global _fernet
    if _fernet is None:
        key = config.SESSION_ENCRYPT_KEY
        if not key:
            raise RuntimeError("SESSION_ENCRYPT_KEY is not set in the environment")
        _fernet = Fernet(key.encode() if isinstance(key, str) else key)
    return _fernet


def has_session(user_id: int) -> bool:
    """Return True if *user_id* has a session (file or legacy JSON)."""
    return session_file(user_id).exists() or legacy_file(user_id).exists()


def _legacy_session_string(user_id: int) -> str | None:
    """Decrypt and return the StringSession payload from a legacy JSON file."""
    legacy = legacy_file(user_id)
    if not legacy.exists():
        return None
    try:
        data = json.loads(legacy.read_text(encoding="utf-8"))
        encrypted = data.get("session")
        if not encrypted:
            return None
        return _fernet_cipher().decrypt(encrypted.encode()).decode()
    except Exception as exc:
        logger.warning("Failed to read legacy session", user_id=user_id, error=str(exc))
        return None


def _string_session_to_file(session_string: str, target: Path) -> bool:
    """Write an equivalent SQLite file session from a Telethon session string."""
    try:
        memory = StringSession(session_string)  # decoded eagerly in __init__
        if not memory.auth_key:
            return False

        db = SQLiteSession(str(target))
        db.set_dc(memory.dc_id, memory.server_address, memory.port)
        db.auth_key = memory.auth_key
        db.save()
        db.close()
        return target.exists() and target.stat().st_size > 0
    except Exception as exc:
        logger.warning("StringSession -> file migration failed", error=str(exc))
        return False


def migrate_legacy(user_id: int) -> bool:
    """Convert a legacy StringSession JSON to a file session. Returns success.

    Any stale legacy JSON is removed when a file session already exists.
    """
    target = session_file(user_id)
    if target.exists():
        try:
            legacy_file(user_id).unlink()
        except OSError:
            pass
        return True

    session_string = _legacy_session_string(user_id)
    if not session_string:
        return False

    ok = _string_session_to_file(session_string, target)
    if ok:
        logger.info("Migrated legacy session to file session", user_id=user_id)
        try:
            legacy_file(user_id).unlink()
        except OSError:
            pass
    return ok


# ── Client construction ───────────────────────────────────────────

def build_client(user_id: int) -> TelegramClient | None:
    """Return a file-session-backed client for *user_id*, or None."""
    file_path = session_file(user_id)
    if not file_path.exists():
        if not migrate_legacy(user_id):
            return None
    return TelegramClient(
        SQLiteSession(str(file_path)),
        int(config.TG_API_ID),
        config.TG_API_HASH,
    )


def create_login_client(user_id: int) -> TelegramClient:
    """Return a new client bound to a fresh file session for the login flow.

    Any previous session for *user_id* is removed so a re-login starts clean.
    """
    paths.SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
    for stale in (session_file(user_id), legacy_file(user_id)):
        try:
            stale.unlink()
        except OSError:
            pass
    return TelegramClient(
        SQLiteSession(str(session_file(user_id))),
        int(config.TG_API_ID),
        config.TG_API_HASH,
    )


# ── Client lifecycle ──────────────────────────────────────────────

async def start_client(user_id: int) -> bool:
    """Connect and cache the user's file-session client.

    Returns True when the client is running (started or already active).
    """
    existing = _active_clients.get(user_id)
    if existing is not None and existing.is_connected():
        return True

    client = build_client(user_id)
    if client is None:
        return False

    try:
        await client.connect()
        if not await client.is_user_authorized():
            await client.disconnect()
            logger.warning("User session not authorized", user_id=user_id)
            return False

        _active_clients[user_id] = client

        from handler.outgoing import register_outgoing_handlers
        register_outgoing_handlers(client, user_id)
        await client.get_dialogs()

        logger.info("User client started", user_id=user_id)
        return True
    except Exception as exc:
        logger.error("Failed to start user client", user_id=user_id, error=str(exc))
        return False


async def stop_client(user_id: int) -> bool:
    client = _active_clients.pop(user_id, None)
    if client is None:
        return False
    try:
        await client.disconnect()
    except Exception:
        pass
    logger.info("User client stopped", user_id=user_id)
    return True


def get_client(user_id: int) -> TelegramClient | None:
    client = _active_clients.get(user_id)
    return client if client is not None and client.is_connected() else None


def is_client_active(user_id: int) -> bool:
    return get_client(user_id) is not None


async def stop_all_clients() -> None:
    for user_id in list(_active_clients):
        await stop_client(user_id)


async def remove_session(user_id: int) -> bool:
    """Stop the client and delete every stored session artifact."""
    await stop_client(user_id)
    removed = False
    for candidate in (session_file(user_id), legacy_file(user_id)):
        try:
            candidate.unlink()
            removed = True
        except OSError:
            pass
    if removed:
        logger.info("User session removed", user_id=user_id)
    return removed
