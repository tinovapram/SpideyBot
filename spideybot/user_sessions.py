"""
SpideyBot — User Session Manager.

Encrypts / decrypts Telethon session strings with Fernet, stores them
as files in the user_sessions/ directory, and manages per-user
TelegramClient instances for accessing private content.
"""

import json
import os

import structlog
from cryptography.fernet import Fernet
from telethon import TelegramClient
from telethon.sessions import StringSession

from spideybot import config

logger = structlog.get_logger(__name__)

# ─── Paths ─────────────────────────────────────────────────────────

_SESSIONS_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "user_sessions",
)

# ─── Fernet (lazy singleton) ──────────────────────────────────────

_fernet: Fernet | None = None


def _get_fernet() -> Fernet:
    """Return (and cache) a Fernet cipher from SESSION_ENCRYPT_KEY."""
    global _fernet
    if _fernet is None:
        key = config.SESSION_ENCRYPT_KEY
        if not key:
            raise RuntimeError("SESSION_ENCRYPT_KEY is not set in the environment")
        _fernet = Fernet(key.encode() if isinstance(key, str) else key)
    return _fernet


# ─── Low-level helpers ─────────────────────────────────────────────

def encrypt_session(plaintext: str) -> str:
    """Encrypt a Telethon StringSession string for storage."""
    return _get_fernet().encrypt(plaintext.encode()).decode()


def decrypt_session(ciphertext: str) -> str:
    """Decrypt a stored session string back to plaintext."""
    return _get_fernet().decrypt(ciphertext.encode()).decode()


def _session_path(user_id: int) -> str:
    """Return the file path for a user's session."""
    return os.path.join(_SESSIONS_DIR, f"{user_id}.json")


def _ensure_dir() -> None:
    os.makedirs(_SESSIONS_DIR, exist_ok=True)


# ─── Active client registry ────────────────────────────────────────
# Maps user_id -> connected TelegramClient for user accounts.
_active_clients: dict[int, TelegramClient] = {}


# ─── Public API — File I/O ─────────────────────────────────────────

def get_or_none(user_id: int) -> str | None:
    """Return the raw encrypted session string for *user_id*, or ``None``."""
    path = _session_path(user_id)
    if not os.path.isfile(path):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data.get("session")
    except (json.JSONDecodeError, OSError):
        return None


def save(user_id: int, phone: str, session_string: str) -> None:
    """Encrypt and persist a freshly-created Telethon session as a file."""
    _ensure_dir()
    encrypted = encrypt_session(session_string)
    path = _session_path(user_id)
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"phone": phone, "session": encrypted}, f)
    logger.info("User session saved to file", user_id=user_id, path=path)


def remove(user_id: int) -> bool:
    """Delete a user's stored session file.  Returns True if a file was removed."""
    stop_client(user_id)
    path = _session_path(user_id)
    if os.path.isfile(path):
        os.remove(path)
        logger.info("User session file removed", user_id=user_id, path=path)
        return True
    return False


# ─── Public API — Client Lifecycle ─────────────────────────────────

def create_user_client(encrypted_session: str) -> TelegramClient:
    """Build a Telethon client from an encrypted session string.

    The caller is responsible for disconnecting the client when done.
    """
    session_str = decrypt_session(encrypted_session)
    return TelegramClient(
        StringSession(session_str),
        int(config.TG_API_ID),
        config.TG_API_HASH,
    )


async def start_client(user_id: int) -> bool:
    """Connect and cache the user's TelegramClient.

    Returns True if the client is running (started or already active).
    Returns False if no saved session exists or connection failed.
    """
    if user_id in _active_clients and _active_clients[user_id].is_connected():
        return True

    encrypted = get_or_none(user_id)
    if not encrypted:
        return False

    try:
        client = create_user_client(encrypted)
        await client.connect()
        if not await client.is_user_authorized():
            await client.disconnect()
            logger.warning("User session not authorized", user_id=user_id)
            return False
        _active_clients[user_id] = client
        me = await client.get_me()
        logger.info("User client started", user_id=user_id, name=me.first_name)

        # ── Register outgoing command handlers on user's client ───
        from spideybot.core.handlers.outgoing import register_outgoing_handlers
        register_outgoing_handlers(client, user_id)
        _ = await client.get_dialogs()
        return True
    except Exception as e:
        logger.error("Failed to start user client", user_id=user_id, error=str(e))
        return False


async def stop_client(user_id: int) -> bool:
    """Disconnect and remove the user's TelegramClient.

    Returns True if a client was stopped, False if none was running.
    """
    client = _active_clients.pop(user_id, None)
    if client is None:
        return False
    try:
        await client.disconnect()
    except Exception:
        pass
    logger.info("User client stopped", user_id=user_id)
    return True


def is_client_active(user_id: int) -> bool:
    """Return True if the user's client is currently connected."""
    client = _active_clients.get(user_id)
    return client is not None and client.is_connected()


def get_client(user_id: int) -> TelegramClient | None:
    """Return the active client for *user_id*, or None."""
    client = _active_clients.get(user_id)
    if client and client.is_connected():
        return client
    return None


async def stop_all_clients() -> None:
    """Disconnect every active user client (called on bot shutdown)."""
    for uid in list(_active_clients):
        await stop_client(uid)
