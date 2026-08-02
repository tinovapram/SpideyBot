"""
SpideyBot — Database Module.

SQLite-backed user management via SQLAlchemy ORM with in-memory caching.
"""

import time
import os
from typing import Optional, Tuple

from spideybot import models

DB_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "data",
    "bot_database.db",
)

# ─── In-memory cache ──────────────────────────────────────────────

class _CachedUser:
    """Lightweight mirror of a User row kept in memory for fast reads."""

    __slots__ = ("user_id", "username", "is_premium", "premium_expiry")

    def __init__(self, user_id: int, username: str | None, is_premium: bool, premium_expiry: int):
        self.user_id = user_id
        self.username = username
        self.is_premium = is_premium
        self.premium_expiry = premium_expiry

_user_cache: dict[int, _CachedUser] = {}


def _to_cached(row: models.User) -> _CachedUser:
    return _CachedUser(row.user_id, row.username, row.is_premium, row.premium_expiry)


# ─── Init ──────────────────────────────────────────────────────────

def init_db() -> None:
    """Create the data directory and all ORM tables."""
    models.init_models()


# ─── Internal Helpers ──────────────────────────────────────────────

def _get_user_from_db(user_id: int) -> _CachedUser | None:
    """Load a user from the database by user ID."""
    with models.get_session() as sess:
        row = sess.get(models.User, user_id)
        if row:
            return _to_cached(row)
    return None


def _save_user_to_db(user: _CachedUser) -> None:
    """Insert or update a user row via SQLAlchemy."""
    with models.get_session() as sess:
        existing = sess.get(models.User, user.user_id)
        if existing:
            existing.username = user.username
            existing.is_premium = user.is_premium
            existing.premium_expiry = user.premium_expiry
        else:
            sess.add(models.User(
                user_id=user.user_id,
                username=user.username,
                is_premium=user.is_premium,
                premium_expiry=user.premium_expiry,
            ))
        sess.commit()


def _resolve_user_id(user_id_or_username: str) -> int | None:
    """
    Resolve a user identifier (numeric ID string or @username) to an integer user_id.

    Checks the in-memory cache first, then falls back to the database.
    """
    if user_id_or_username.isdigit():
        return int(user_id_or_username)

    clean = user_id_or_username.lstrip("@")

    # Check cache
    for uid, user in _user_cache.items():
        if user.username and user.username.lower() == clean.lower():
            return uid

    # Check DB
    with models.get_session() as sess:
        row = sess.query(models.User.user_id).filter(
            models.User.username.ilike(clean)
        ).first()
        return row[0] if row else None


# ─── Public API ────────────────────────────────────────────────────

# Re-export the legacy name so existing test imports keep working.
UserInfo = _CachedUser


def save_or_update_user(user_id: int, username: str):
    """Save a new user or update an existing user's username."""
    clean_username = username.lstrip("@") if username else None

    user = _user_cache.get(user_id)
    if user:
        if user.username != clean_username:
            user.username = clean_username
            _save_user_to_db(user)
    else:
        user = _get_user_from_db(user_id)
        if user:
            if user.username != clean_username:
                user.username = clean_username
                _save_user_to_db(user)
        else:
            user = _CachedUser(user_id, clean_username, False, 0)
            _save_user_to_db(user)
        _user_cache[user_id] = user


def is_user_premium(user_id: int) -> bool:
    """Check if a user has active premium status (not expired)."""
    user = _user_cache.get(user_id)
    if not user:
        user = _get_user_from_db(user_id)
        if not user:
            return False
        _user_cache[user_id] = user

    if user.is_premium:
        now = int(time.time())
        if user.premium_expiry == 0 or user.premium_expiry > now:
            return True
        else:
            user.is_premium = False
            _save_user_to_db(user)
            return False
    return False


def add_premium_by_id(user_id: int, days: int) -> int:
    """Grant or extend premium status.  Returns the new expiry timestamp."""
    now = int(time.time())
    user = _user_cache.get(user_id)
    if not user:
        user = _get_user_from_db(user_id)

    if user:
        current_expiry = user.premium_expiry or 0
        new_expiry = max(now, current_expiry) + days * 86400
        user.is_premium = True
        user.premium_expiry = new_expiry
    else:
        new_expiry = now + days * 86400
        user = _CachedUser(user_id, None, True, new_expiry)

    _user_cache[user_id] = user
    _save_user_to_db(user)
    return new_expiry


def add_premium_by_username(username: str, days: int) -> Tuple[bool, Optional[int], Optional[int]]:
    """Grant premium by username.  Returns (success, user_id, expiry)."""
    user_id = _resolve_user_id(username)
    if user_id is None:
        return False, None, None
    expiry = add_premium_by_id(user_id, days)
    return True, user_id, expiry


def remove_premium(user_id_or_username: str) -> Tuple[bool, str, Optional[int]]:
    """Revoke premium.  Returns (success, message, user_id)."""
    user_id = _resolve_user_id(user_id_or_username)
    if not user_id:
        return False, "User not found in database.", None

    user = _user_cache.get(user_id)
    if not user:
        user = _get_user_from_db(user_id)

    if user:
        user.is_premium = False
        user.premium_expiry = 0
        _user_cache[user_id] = user
        _save_user_to_db(user)
        return True, "Premium status removed successfully.", user_id

    return False, "User not found in database.", None


def find_user_by_username(username: str) -> Optional[int]:
    """Find a user's ID by their username.  Returns user_id or None."""
    return _resolve_user_id(username)


def check_user_premium_status(user_id_or_username: str) -> str:
    """Return a formatted string describing a user's premium status."""
    user_id = _resolve_user_id(user_id_or_username)
    if not user_id:
        return "❌ User not found in database."

    user = _user_cache.get(user_id)
    if not user:
        user = _get_user_from_db(user_id)
        if not user:
            return "❌ User not found in database."
        _user_cache[user_id] = user

    uname_str = f"@{user.username}" if user.username else "None"

    if user.is_premium:
        if user.premium_expiry == 0:
            return f"👤 User: {uname_str} (ID: {user.user_id})\n✨ Status: Premium (Permanent)"
        else:
            now = int(time.time())
            expiry_date = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(user.premium_expiry))
            if user.premium_expiry > now:
                remaining_days = (user.premium_expiry - now) / 86400
                return f"👤 User: {uname_str} (ID: {user.user_id})\n✨ Status: Premium\n📅 Expiry: {expiry_date} ({remaining_days:.1f} days remaining)"
            else:
                return f"👤 User: {uname_str} (ID: {user.user_id})\n✨ Status: Free (Premium expired on {expiry_date})"
    else:
        return f"👤 User: {uname_str} (ID: {user.user_id})\n✨ Status: Free"
