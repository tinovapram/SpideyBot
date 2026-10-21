"""
User account session manager.

Sessions are stored as Telethon **file sessions** (SQLite) under
``user_sessions/<user_id>.session``. For backward compatibility, legacy
encrypted StringSession files (``user_sessions/<user_id>.json``) are detected
and migrated to the new file format automatically on first use.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import structlog
from cryptography.fernet import Fernet
from telethon import TelegramClient
from telethon.sessions import SQLiteSession, StringSession

from core import config
from utils import paths

logger = structlog.get_logger(__name__)


def _update_metrics() -> None:
    from core.metrics import get_metrics

    get_metrics().set("active_sessions", len(_active_clients))

_active_clients: dict[int, TelegramClient] = {}
_last_used: dict[int, float] = {}
_fernet: Fernet | None = None


# ── Helpers ───────────────────────────────────────────────────────

def session_file(user_id: int) -> Path:
    """Path of the Telethon file session for *user_id*."""
    return paths.SESSIONS_DIR / f"{user_id}.session"


def secure_session_file(user_id: int) -> None:
    """Apply owner-only permissions after Telethon creates the SQLite file."""
    _restrict_file(session_file(user_id))


def legacy_file(user_id: int) -> Path:
    """Path of a legacy encrypted StringSession JSON file."""
    return paths.SESSIONS_DIR / f"{user_id}.json"


def _restrict_file(path: Path) -> None:
    """Best-effort owner-only permissions for session files."""
    try:
        path.chmod(0o600)
    except OSError:
        logger.warning("Unable to restrict session file permissions", path=str(path))


def _fernet_cipher() -> Fernet:
    global _fernet
    if _fernet is None:
        key = config.get_settings().session_encrypt_key
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
        _restrict_file(target)
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
    settings = config.get_settings()
    _restrict_file(file_path)
    return TelegramClient(
        SQLiteSession(str(file_path)),
        int(settings.tg_api_id),
        settings.tg_api_hash,
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
    settings = config.get_settings()
    session_path = session_file(user_id)
    _restrict_file(session_path)
    return TelegramClient(
        SQLiteSession(str(session_path)),
        int(settings.tg_api_id),
        settings.tg_api_hash,
    )


# ── Client lifecycle ──────────────────────────────────────────────

async def start_client(user_id: int) -> bool:
    """Connect and cache the user's file-session client.

    Returns True when the client is running (started or already active).
    """
    existing = _active_clients.get(user_id)
    if existing is not None and existing.is_connected():
        _last_used[user_id] = time.monotonic()
        return True

    max_live = max(1, config.get_settings().session_max_live)
    if len(_active_clients) >= max_live:
        oldest_user = min(_last_used, key=_last_used.get, default=None)
        if oldest_user is not None and oldest_user != user_id:
            await stop_client(oldest_user)
        else:
            logger.warning("Active client limit reached", limit=max_live)
            return False

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
        _last_used[user_id] = time.monotonic()
        _update_metrics()

        # Imported lazily: handler.handler imports core.sessions at module level.
        # While this client is live it is the priority path for /dl and /dt, and
        # the bot side stands down so the command isn't handled twice.
        from handler.handler import get_handler
        get_handler().register_user_handlers(client)
        logger.info("User client started", user_id=user_id)
        return True
    except Exception as exc:
        logger.error("Failed to start user client", user_id=user_id, error=str(exc))
        return False


async def stop_client(user_id: int) -> bool:
    client = _active_clients.pop(user_id, None)
    if client is None:
        _last_used.pop(user_id, None)
        return False
    try:
        await client.disconnect()
    except Exception:
        pass
    _last_used.pop(user_id, None)
    _update_metrics()
    logger.info("User client stopped", user_id=user_id)
    return True


def get_client(user_id: int) -> TelegramClient | None:
    client = _active_clients.get(user_id)
    if client is None or not client.is_connected():
        return None
    _last_used[user_id] = time.monotonic()
    return client


def is_client_active(user_id: int) -> bool:
    return get_client(user_id) is not None


async def evict_idle_clients() -> int:
    """Disconnect clients unused for the configured idle period."""
    idle_seconds = max(1, config.get_settings().session_idle_seconds)
    now = time.monotonic()
    stale = [
        user_id
        for user_id in list(_active_clients)
        if now - _last_used.get(user_id, now) >= idle_seconds
    ]
    for user_id in stale:
        await stop_client(user_id)
    if stale:
        logger.info("Evicted idle user sessions", count=len(stale))
    return len(stale)


def active_count() -> int:
    """Return the number of live user clients."""
    return len(_active_clients)


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
