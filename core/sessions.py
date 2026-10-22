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

from core import config, sendgate
from utils import paths

logger = structlog.get_logger(__name__)


def _update_metrics() -> None:
    from core.metrics import get_metrics

    get_metrics().set("active_sessions", len(_active_clients))

_active_clients: dict[int, TelegramClient] = {}
_last_used: dict[int, float] = {}
_leases: dict[int, int] = {}
_tiers: dict[int, str] = {}
_disconnected_since: dict[int, float] = {}
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
    client = TelegramClient(
        SQLiteSession(str(file_path)),
        int(settings.tg_api_id),
        settings.tg_api_hash,
    )
    sendgate.label_client(client, f"user:{user_id}")
    return client


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

_EVICTION_RANK = {"free": 0, "pro": 1, "premium": 2}


def _eviction_rank(tier: str) -> int:
    """Lower ranks go first: a free client is evicted before a paid one."""
    return _EVICTION_RANK.get(tier, 0)


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
        candidates = [
            uid for uid in _active_clients if uid != user_id and not is_leased(uid)
        ]
        tiers = await _resolve_tiers(candidates) if candidates else {}
        # Tier first, then age: the least-recently-used free client makes room
        # before any paying user does.
        victim = min(
            candidates,
            key=lambda uid: (
                _eviction_rank(tiers.get(uid, "free")),
                _last_used.get(uid, 0.0),
            ),
            default=None,
        )
        if victim is not None:
            logger.info(
                "Evicted client for capacity",
                user_id=victim,
                tier=tiers.get(victim, "free"),
            )
            await stop_client(victim)
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
    _tiers.pop(user_id, None)
    _disconnected_since.pop(user_id, None)
    if client is None:
        _last_used.pop(user_id, None)
        return False
    try:
        await client.disconnect()
    except Exception:
        pass
    # disconnect() only stops the network loop; the SQLite session keeps its
    # file handle until closed, so several login/evict cycles would leak them.
    try:
        client.session.close()
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


# ── Leases ────────────────────────────────────────────────────────
# A job holds a lease for its whole duration so the idle sweep and the LRU
# cap cannot disconnect a client mid-upload.

def acquire(user_id: int) -> None:
    """Pin *user_id*'s client against eviction."""
    _leases[user_id] = _leases.get(user_id, 0) + 1


def release(user_id: int) -> None:
    """Drop one lease taken by :func:`acquire`."""
    remaining = _leases.get(user_id, 0) - 1
    if remaining > 0:
        _leases[user_id] = remaining
    else:
        _leases.pop(user_id, None)
    # The job just used the client; a long run must not look idle afterwards.
    touch(user_id)


def is_leased(user_id: int) -> bool:
    """Return True while a running job holds *user_id*'s client."""
    return _leases.get(user_id, 0) > 0


def touch(user_id: int) -> None:
    """Refresh *user_id*'s idle timer when their client is live."""
    if user_id in _active_clients:
        _last_used[user_id] = time.monotonic()


def _min_idle_seconds(settings) -> int:
    """Smallest configured idle, so young clients skip the tier lookup."""
    values = [
        getattr(settings, "session_idle_seconds", 1800),
        getattr(settings, "session_idle_pro_seconds", 43200),
        getattr(settings, "session_idle_premium_seconds", 86400),
    ]
    return max(1, min(values))


async def _resolve_tiers(user_ids: list[int]) -> dict[int, str]:
    """Map *user_ids* to their effective tier, cached for later sweeps."""
    resolved: dict[int, str] = {}
    try:
        from sqlalchemy import select

        from core.db import session_scope
        from core.models import User
        from core.tiers import effective_tier

        async with session_scope() as session:
            rows = await session.execute(
                select(User.id, User.tier, User.tier_expiry, User.is_admin).where(
                    User.id.in_(user_ids)
                )
            )
            resolved = {
                row.id: effective_tier(row.tier, row.tier_expiry, is_admin=row.is_admin)
                for row in rows
            }
    except Exception as exc:
        logger.warning("Unable to resolve tiers for session eviction", error=str(exc))
    _tiers.update(resolved)
    return {user_id: _tiers.get(user_id, "free") for user_id in user_ids}


async def evict_idle_clients() -> int:
    """Disconnect clients idle past their tier's timeout (skipping leased)."""
    settings = config.get_settings()
    now = time.monotonic()
    candidates = [
        user_id
        for user_id in list(_active_clients)
        if not is_leased(user_id)
        and now - _last_used.get(user_id, now) >= _min_idle_seconds(settings)
    ]
    if not candidates:
        return 0
    tiers = await _resolve_tiers(candidates)
    stale = [
        user_id
        for user_id in candidates
        if now - _last_used.get(user_id, now) >= settings.session_idle_for(tiers[user_id])
    ]
    for user_id in stale:
        await stop_client(user_id)
    if stale:
        logger.info("Evicted idle user sessions", count=len(stale))
    return len(stale)


def active_count() -> int:
    """Return the number of live user clients."""
    return len(_active_clients)


async def drop_disconnected_clients(grace_seconds: float = 60.0) -> int:
    """Forget live clients whose transport died, so they rebuild on demand.

    Telethon reconnects on its own for a short while, so a client is only
    dropped once it has stayed disconnected for *grace_seconds*.
    """
    now = time.monotonic()
    stale: list[int] = []

    for user_id, client in list(_active_clients.items()):
        if is_leased(user_id) or client.is_connected():
            _disconnected_since.pop(user_id, None)
            continue
        since = _disconnected_since.setdefault(user_id, now)
        if now - since >= grace_seconds:
            stale.append(user_id)

    for user_id in stale:
        logger.info("Dropping disconnected user client", user_id=user_id)
        await stop_client(user_id)

    return len(stale)


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
