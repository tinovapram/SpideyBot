"""
SpideyBot — Database Module.

SQLite-backed user management with in-memory caching for premium tier tracking.
"""

import sqlite3
import time
import os
from typing import Optional, Tuple, Dict

DB_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "bot_database.db")


class UserInfo:
    """Represents a cached user record."""

    def __init__(self, user_id: int, username: Optional[str], is_premium: bool, premium_expiry: int):
        self.user_id = user_id
        self.username = username
        self.is_premium = is_premium
        self.premium_expiry = premium_expiry


# In-memory user cache to avoid DB reads on every operation
_user_cache: Dict[int, UserInfo] = {}


def init_db() -> None:
    """Initialize the database and create tables if they don't exist."""
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            is_premium INTEGER DEFAULT 0,
            premium_expiry INTEGER DEFAULT 0
        )
    ''')
    conn.commit()
    conn.close()


# ─── Internal Helpers ────────────────────────────────────────────────

def _get_user_from_db(user_id: int) -> Optional[UserInfo]:
    """Load a user record from the database by user ID."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('SELECT user_id, username, is_premium, premium_expiry FROM users WHERE user_id = ?', (user_id,))
    row = cursor.fetchone()
    conn.close()
    if row:
        return UserInfo(row[0], row[1], bool(row[2]), row[3])
    return None


def _save_user_to_db(user: UserInfo) -> None:
    """Insert or update a user record in the database."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('''
        INSERT INTO users (user_id, username, is_premium, premium_expiry)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(user_id) DO UPDATE SET 
            username = EXCLUDED.username,
            is_premium = EXCLUDED.is_premium,
            premium_expiry = EXCLUDED.premium_expiry
    ''', (user.user_id, user.username, 1 if user.is_premium else 0, user.premium_expiry))
    conn.commit()
    conn.close()


def _resolve_user_id(user_id_or_username: str) -> Optional[int]:
    """
    Resolve a user identifier (numeric ID string or @username) to an integer user_id.

    Checks the in-memory cache first, then falls back to the database.

    Args:
        user_id_or_username: A numeric user ID string or a Telegram username.

    Returns:
        The integer user_id, or None if not found.
    """
    if user_id_or_username.isdigit():
        return int(user_id_or_username)

    clean_username = user_id_or_username.lstrip('@')

    # Check cache first
    for uid, user in _user_cache.items():
        if user.username and user.username.lower() == clean_username.lower():
            return uid

    # Check DB if not in cache
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('SELECT user_id FROM users WHERE username LIKE ?', (clean_username,))
    row = cursor.fetchone()
    conn.close()

    return row[0] if row else None


# ─── Public API ──────────────────────────────────────────────────────

def save_or_update_user(user_id: int, username: str):
    """Save a new user or update an existing user's username."""
    clean_username = username.lstrip('@') if username else None

    # Check cache first
    user = _user_cache.get(user_id)
    if user:
        if user.username != clean_username:
            user.username = clean_username
            _save_user_to_db(user)
    else:
        # Load from DB or create new
        user = _get_user_from_db(user_id)
        if user:
            if user.username != clean_username:
                user.username = clean_username
                _save_user_to_db(user)
        else:
            user = UserInfo(user_id, clean_username, False, 0)
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
            # Expired! Update cache and DB
            user.is_premium = False
            _save_user_to_db(user)
            return False
    return False


def add_premium_by_id(user_id: int, days: int) -> int:
    """
    Grant or extend premium status for a user by their ID.

    Returns:
        The new premium expiry timestamp.
    """
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
        user = UserInfo(user_id, None, True, new_expiry)

    _user_cache[user_id] = user
    _save_user_to_db(user)
    return new_expiry


def add_premium_by_username(username: str, days: int) -> Tuple[bool, Optional[int], Optional[int]]:
    """
    Grant or extend premium status for a user by their username.

    Returns:
        (success, user_id, expiry_timestamp)
    """
    user_id = _resolve_user_id(username)
    if user_id is None:
        return False, None, None

    expiry = add_premium_by_id(user_id, days)
    return True, user_id, expiry


def remove_premium(user_id_or_username: str) -> Tuple[bool, str, Optional[int]]:
    """
    Revoke premium status from a user.

    Returns:
        (success, message, user_id)
    """
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
    """
    Find a user's ID by their username.

    This is the public interface for username->ID resolution,
    replacing inline sqlite3 queries in other modules.

    Returns:
        The user_id, or None if not found.
    """
    return _resolve_user_id(username)


def check_user_premium_status(user_id_or_username: str) -> str:
    """
    Get a formatted string describing a user's premium status.

    Returns:
        A Telegram-formatted status message string.
    """
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
            expiry_date = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(user.premium_expiry))
            if user.premium_expiry > now:
                remaining_days = (user.premium_expiry - now) / 86400
                return f"👤 User: {uname_str} (ID: {user.user_id})\n✨ Status: Premium\n📅 Expiry: {expiry_date} ({remaining_days:.1f} days remaining)"
            else:
                return f"👤 User: {uname_str} (ID: {user.user_id})\n✨ Status: Free (Premium expired on {expiry_date})"
    else:
        return f"👤 User: {uname_str} (ID: {user.user_id})\n✨ Status: Free"
