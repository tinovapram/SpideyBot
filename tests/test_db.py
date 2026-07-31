"""
Tests for spideybot/db.py — SQLite user management with in-memory caching.

Uses a temporary database for each test to avoid touching production data.
"""

import os
import sys
import time
import tempfile
import sqlite3
import importlib

import pytest

# Add project root to path
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)


@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch):
    """
    Override DB_PATH to a temp file and clear the in-memory cache
    before every test.
    """
    db_file = tmp_path / "test_bot_database.db"
    monkeypatch.setattr("spideybot.db.DB_PATH", str(db_file))

    # Reset the module-level cache
    import spideybot.db as db_mod
    db_mod._user_cache.clear()

    # Reinitialize the DB for each test
    db_mod.init_db()

    yield db_mod

    # Cleanup: clear cache after test
    db_mod._user_cache.clear()


# ════════════════════════════════════════════════════════════════════
# init_db
# ════════════════════════════════════════════════════════════════════

class TestInitDb:
    def test_creates_table(self, isolated_db, tmp_path):
        """init_db creates the users table."""
        conn = sqlite3.connect(str(tmp_path / "test_bot_database.db"))
        cursor = conn.cursor()
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
        tables = [row[0] for row in cursor.fetchall()]
        conn.close()
        assert "users" in tables

    def test_idempotent(self, isolated_db):
        """Calling init_db twice does not raise or duplicate."""
        isolated_db.init_db()
        isolated_db.init_db()  # should not raise

    def test_creates_data_directory(self, monkeypatch, tmp_path):
        """init_db creates the data directory if it doesn't exist."""
        nested_db = tmp_path / "nested" / "data" / "test.db"
        monkeypatch.setattr("spideybot.db.DB_PATH", str(nested_db))
        isolated_db_init = importlib.import_module("spideybot.db")
        isolated_db_init.init_db()
        assert nested_db.parent.exists()


# ════════════════════════════════════════════════════════════════════
# save_or_update_user
# ════════════════════════════════════════════════════════════════════

class TestSaveOrUpdateUser:
    def test_create_new_user(self, isolated_db):
        """Saving a new user creates a DB record and cache entry."""
        isolated_db.save_or_update_user(12345, "alice")
        user = isolated_db._user_cache.get(12345)
        assert user is not None
        assert user.username == "alice"
        assert user.is_premium is False
        assert user.premium_expiry == 0

    def test_update_username(self, isolated_db):
        """Saving an existing user updates their username."""
        isolated_db.save_or_update_user(12345, "alice")
        isolated_db.save_or_update_user(12345, "alice_v2")
        user = isolated_db._user_cache.get(12345)
        assert user.username == "alice_v2"

    def test_strip_at_from_username(self, isolated_db):
        """Leading @ in username is stripped."""
        isolated_db.save_or_update_user(12345, "@alice")
        user = isolated_db._user_cache.get(12345)
        assert user.username == "alice"

    def test_none_username(self, isolated_db):
        """Saving with None username works."""
        isolated_db.save_or_update_user(12345, None)
        user = isolated_db._user_cache.get(12345)
        assert user.username is None

    def test_persists_to_database(self, isolated_db):
        """After save, user can be read back from the database."""
        isolated_db.save_or_update_user(12345, "alice")
        loaded = isolated_db._get_user_from_db(12345)
        assert loaded is not None
        assert loaded.username == "alice"

    def test_overwrite_via_upsert(self, isolated_db):
        """Saving the same user_id replaces the old record."""
        isolated_db.save_or_update_user(12345, "alice")
        isolated_db.save_or_update_user(12345, "bob")
        loaded = isolated_db._get_user_from_db(12345)
        assert loaded.username == "bob"


# ════════════════════════════════════════════════════════════════════
# is_user_premium
# ════════════════════════════════════════════════════════════════════

class TestIsUserPremium:
    def test_nonexistent_user(self, isolated_db):
        """Unknown user is not premium."""
        assert isolated_db.is_user_premium(99999) is False

    def test_free_user(self, isolated_db):
        """A saved but non-premium user is not premium."""
        isolated_db.save_or_update_user(12345, "alice")
        assert isolated_db.is_user_premium(12345) is False

    def test_premium_active(self, isolated_db):
        """User with future expiry is premium."""
        isolated_db.save_or_update_user(12345, "alice")
        isolated_db.add_premium_by_id(12345, 30)
        assert isolated_db.is_user_premium(12345) is True

    def test_premium_lifetime(self, isolated_db):
        """User with expiry=0 after being premium is lifetime."""
        # Manually set up lifetime premium
        from spideybot.db import UserInfo
        user = UserInfo(12345, "alice", True, 0)
        isolated_db._user_cache[12345] = user
        isolated_db._save_user_to_db(user)
        # expiry=0 means lifetime
        assert isolated_db.is_user_premium(12345) is True

    def test_premium_expired(self, isolated_db):
        """User with past expiry is no longer premium."""
        from spideybot.db import UserInfo
        past = int(time.time()) - 86400  # yesterday
        user = UserInfo(12345, "alice", True, past)
        isolated_db._user_cache[12345] = user
        isolated_db._save_user_to_db(user)
        assert isolated_db.is_user_premium(12345) is False
        # Verify DB was updated too
        from_db = isolated_db._get_user_from_db(12345)
        assert from_db.is_premium is False

    def test_premium_not_expired(self, isolated_db):
        """User with future expiry is still premium."""
        from spideybot.db import UserInfo
        future = int(time.time()) + 86400 * 30
        user = UserInfo(12345, "alice", True, future)
        isolated_db._user_cache[12345] = user
        isolated_db._save_user_to_db(user)
        assert isolated_db.is_user_premium(12345) is True


# ════════════════════════════════════════════════════════════════════
# add_premium_by_id
# ════════════════════════════════════════════════════════════════════

class TestAddPremiumById:
    def test_new_user_gets_premium(self, isolated_db):
        """Adding premium to unknown user creates and marks premium."""
        expiry = isolated_db.add_premium_by_id(12345, 30)
        now = int(time.time())
        assert expiry > now
        assert isolated_db.is_user_premium(12345) is True

    def test_existing_user_extends(self, isolated_db):
        """Adding premium to existing user extends from current expiry."""
        isolated_db.save_or_update_user(12345, "alice")
        expiry1 = isolated_db.add_premium_by_id(12345, 30)
        expiry2 = isolated_db.add_premium_by_id(12345, 10)
        assert expiry2 > expiry1

    def test_returns_expiry_timestamp(self, isolated_db):
        """Return value is a valid future timestamp."""
        expiry = isolated_db.add_premium_by_id(12345, 7)
        now = int(time.time())
        assert expiry > now
        assert expiry <= now + 7 * 86400 + 10  # small tolerance

    def test_cached_after_add(self, isolated_db):
        """After add_premium_by_id, user is in cache."""
        isolated_db.add_premium_by_id(12345, 30)
        user = isolated_db._user_cache.get(12345)
        assert user is not None
        assert user.is_premium is True


# ════════════════════════════════════════════════════════════════════
# add_premium_by_username
# ════════════════════════════════════════════════════════════════════

class TestAddPremiumByUsername:
    def test_existing_username(self, isolated_db):
        """Grant premium by known username."""
        isolated_db.save_or_update_user(12345, "alice")
        success, uid, expiry = isolated_db.add_premium_by_username("alice", 30)
        assert success is True
        assert uid == 12345
        assert expiry is not None

    def test_existing_at_username(self, isolated_db):
        """Grant premium by @username."""
        isolated_db.save_or_update_user(12345, "alice")
        success, uid, expiry = isolated_db.add_premium_by_username("@alice", 30)
        assert success is True
        assert uid == 12345

    def test_unknown_username(self, isolated_db):
        """Unknown username returns failure."""
        success, uid, expiry = isolated_db.add_premium_by_username("nobody", 30)
        assert success is False
        assert uid is None
        assert expiry is None

    def test_numeric_id_as_string(self, isolated_db):
        """Numeric string is treated as user_id directly."""
        success, uid, expiry = isolated_db.add_premium_by_username("12345", 30)
        assert success is True
        assert uid == 12345


# ════════════════════════════════════════════════════════════════════
# _resolve_user_id
# ════════════════════════════════════════════════════════════════════

class TestResolveUserId:
    def test_numeric_string(self, isolated_db):
        """Numeric string returns integer directly."""
        result = isolated_db._resolve_user_id("12345")
        assert result == 12345

    def test_at_username_cache(self, isolated_db):
        """@username resolves from cache."""
        isolated_db.save_or_update_user(12345, "alice")
        result = isolated_db._resolve_user_id("@alice")
        assert result == 12345

    def test_at_username_db(self, isolated_db):
        """@username resolves from DB (not in cache)."""
        isolated_db.save_or_update_user(12345, "alice")
        isolated_db._user_cache.clear()  # force DB lookup
        result = isolated_db._resolve_user_id("@alice")
        assert result == 12345

    def test_case_insensitive(self, isolated_db):
        """Username match is case-insensitive."""
        isolated_db.save_or_update_user(12345, "Alice")
        result = isolated_db._resolve_user_id("alice")
        assert result == 12345

    def test_unknown_returns_none(self, isolated_db):
        """Unknown username returns None."""
        result = isolated_db._resolve_user_id("nobody")
        assert result is None


# ════════════════════════════════════════════════════════════════════
# remove_premium
# ════════════════════════════════════════════════════════════════════

class TestRemovePremium:
    def test_remove_existing_user(self, isolated_db):
        """Remove premium from known user."""
        isolated_db.save_or_update_user(12345, "alice")
        isolated_db.add_premium_by_id(12345, 30)
        success, msg, uid = isolated_db.remove_premium("12345")
        assert success is True
        assert uid == 12345
        assert isolated_db.is_user_premium(12345) is False

    def test_remove_by_username(self, isolated_db):
        """Remove premium by @username."""
        isolated_db.save_or_update_user(12345, "alice")
        isolated_db.add_premium_by_id(12345, 30)
        success, msg, uid = isolated_db.remove_premium("@alice")
        assert success is True
        assert isolated_db.is_user_premium(12345) is False

    def test_remove_unknown_user(self, isolated_db):
        """Removing from unknown user returns failure."""
        success, msg, uid = isolated_db.remove_premium("99999")
        assert success is False
        assert "not found" in msg.lower()
        assert uid is None
