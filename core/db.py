"""
Data access layer for user records.

SQLite-backed via SQLAlchemy with an in-memory cache for fast reads.
"""

from __future__ import annotations

import time

from core import models

_user_cache: dict[int, "_CachedUser"] = {}
_username_index: dict[str, int] = {}


class _CachedUser:
    """Lightweight mirror of a ``User`` row kept in memory."""

    __slots__ = ("user_id", "username", "is_premium", "premium_expiry")

    def __init__(
        self,
        user_id: int,
        username: str | None,
        is_premium: bool,
        premium_expiry: int,
    ) -> None:
        self.user_id = user_id
        self.username = username
        self.is_premium = is_premium
        self.premium_expiry = premium_expiry


def _index(user: "_CachedUser") -> None:
    if user.username:
        _username_index[user.username.lower()] = user.user_id


def _unindex(user: "_CachedUser") -> None:
    if user.username:
        _username_index.pop(user.username.lower(), None)


def _to_cached(row: models.User) -> "_CachedUser":
    return _CachedUser(row.user_id, row.username, row.is_premium, row.premium_expiry)


# ── Init ─────────────────────────────────────────────────────────

def init_db() -> None:
    models.init_models()


# ── Internal helpers ─────────────────────────────────────────────

def _get_user_from_db(user_id: int) -> "_CachedUser" | None:
    with models.get_session() as session:
        row = session.get(models.User, user_id)
        return _to_cached(row) if row else None


def _save_user_to_db(user: "_CachedUser") -> None:
    with models.get_session() as session:
        existing = session.get(models.User, user.user_id)
        if existing:
            existing.username = user.username
            existing.is_premium = user.is_premium
            existing.premium_expiry = user.premium_expiry
        else:
            session.add(
                models.User(
                    user_id=user.user_id,
                    username=user.username,
                    is_premium=user.is_premium,
                    premium_expiry=user.premium_expiry,
                )
            )
        session.commit()


def _resolve_user_id(identifier: str) -> int | None:
    """Resolve a numeric ID or ``@username`` to an integer user ID."""
    if identifier.isdigit():
        return int(identifier)

    clean = identifier.lstrip("@").lower()
    user_id = _username_index.get(clean)
    if user_id is not None:
        return user_id

    with models.get_session() as session:
        row = (
            session.query(models.User.user_id)
            .filter(models.User.username.ilike(clean))
            .first()
        )
        return row[0] if row else None


# ── Public API ───────────────────────────────────────────────────

def save_or_update_user(user_id: int, username: str | None) -> None:
    """Create a user record or refresh its username."""
    clean = username.lstrip("@") if username else None

    user = _user_cache.get(user_id)
    if user is not None:
        if user.username != clean:
            _unindex(user)
            user.username = clean
            _index(user)
            _save_user_to_db(user)
        return

    user = _get_user_from_db(user_id)
    if user is not None:
        if user.username != clean:
            _unindex(user)
            user.username = clean
            _index(user)
            _save_user_to_db(user)
    else:
        user = _CachedUser(user_id, clean, False, 0)
        _save_user_to_db(user)

    _user_cache[user_id] = user
    _index(user)


def is_user_premium(user_id: int) -> bool:
    """Return True when the user has an active (non-expired) premium status."""
    user = _user_cache.get(user_id)
    if user is None:
        user = _get_user_from_db(user_id)
        if user is None:
            return False
        _user_cache[user_id] = user
        _index(user)

    if not user.is_premium:
        return False

    now = int(time.time())
    if user.premium_expiry == 0 or user.premium_expiry > now:
        return True

    user.is_premium = False
    _save_user_to_db(user)
    return False


def add_premium_by_id(user_id: int, days: int) -> int:
    """Grant or extend premium. Returns the new expiry timestamp."""
    now = int(time.time())
    user = _user_cache.get(user_id) or _get_user_from_db(user_id)

    if user is None:
        user = _CachedUser(user_id, None, False, 0)

    current_expiry = user.premium_expiry or 0
    user.is_premium = True
    user.premium_expiry = max(now, current_expiry) + days * 86400

    _user_cache[user_id] = user
    _index(user)
    _save_user_to_db(user)
    return user.premium_expiry


def add_premium_by_username(username: str, days: int) -> tuple[bool, int | None, int | None]:
    """Grant premium by username. Returns ``(success, user_id, expiry)``."""
    user_id = _resolve_user_id(username)
    if user_id is None:
        return False, None, None
    return True, user_id, add_premium_by_id(user_id, days)


def remove_premium(identifier: str) -> tuple[bool, str, int | None]:
    """Revoke premium. Returns ``(success, message, user_id)``."""
    user_id = _resolve_user_id(identifier)
    if user_id is None:
        return False, "User not found in database.", None

    user = _user_cache.get(user_id) or _get_user_from_db(user_id)
    if user is None:
        return False, "User not found in database.", None

    user.is_premium = False
    user.premium_expiry = 0
    _user_cache[user_id] = user
    _save_user_to_db(user)
    return True, "Premium status removed successfully.", user_id


def find_user_by_username(username: str) -> int | None:
    """Return a user ID for ``username``, or None."""
    return _resolve_user_id(username)


def check_user_premium_status(identifier: str) -> str:
    """Return a human-readable premium status string for ``identifier``."""
    user_id = _resolve_user_id(identifier)
    if user_id is None:
        return f"👤 User `{identifier}` not found in the database."

    if is_user_premium(user_id):
        cached = _user_cache.get(user_id)
        expiry = cached.premium_expiry if cached else 0
        expiry_str = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(expiry))
        return f"👤 User: `{identifier}`\n✨ Status: Premium (expires `{expiry_str}`)"
    return f"👤 User: `{identifier}`\n✨ Status: Free"
