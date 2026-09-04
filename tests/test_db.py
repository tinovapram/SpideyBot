"""Tests for core.db — SQLite user management with in-memory cache."""

import sqlite3

import pytest


@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch):
    db_file = tmp_path / "test_bot_database.db"
    monkeypatch.setattr("utils.paths.DB_PATH", db_file)

    import core.models as models_mod
    models_mod._engine = None
    models_mod._SessionLocal = None

    import core.db as db_mod
    db_mod._user_cache.clear()
    db_mod._username_index.clear()

    db_mod.init_db()
    yield db_mod, db_file

    db_mod._user_cache.clear()
    db_mod._username_index.clear()
    models_mod._engine = None
    models_mod._SessionLocal = None


class TestInitDb:
    def test_creates_table(self, isolated_db):
        db_mod, db_file = isolated_db
        conn = sqlite3.connect(str(db_file))
        tables = [row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")]
        conn.close()
        assert "users" in tables

    def test_idempotent(self, isolated_db):
        db_mod, _ = isolated_db
        db_mod.init_db()
        db_mod.init_db()


class TestSaveOrUpdateUser:
    def test_create_new_user(self, isolated_db):
        db_mod, _ = isolated_db
        db_mod.save_or_update_user(12345, "alice")
        user = db_mod._user_cache[12345]
        assert user.username == "alice"
        assert user.is_premium is False

    def test_update_username(self, isolated_db):
        db_mod, _ = isolated_db
        db_mod.save_or_update_user(12345, "alice")
        db_mod.save_or_update_user(12345, "alice_v2")
        assert db_mod._user_cache[12345].username == "alice_v2"

    def test_strip_at(self, isolated_db):
        db_mod, _ = isolated_db
        db_mod.save_or_update_user(12345, "@alice")
        assert db_mod._user_cache[12345].username == "alice"

    def test_persists(self, isolated_db):
        db_mod, _ = isolated_db
        db_mod.save_or_update_user(12345, "alice")
        assert db_mod._get_user_from_db(12345).username == "alice"


class TestPremium:
    def test_grant_and_check(self, isolated_db):
        db_mod, _ = isolated_db
        expiry = db_mod.add_premium_by_id(12345, days=30)
        assert expiry > 0
        assert db_mod.is_user_premium(12345) is True

    def test_non_premium(self, isolated_db):
        db_mod, _ = isolated_db
        db_mod.save_or_update_user(999, "freebie")
        assert db_mod.is_user_premium(999) is False

    def test_resolve_username(self, isolated_db):
        db_mod, _ = isolated_db
        db_mod.save_or_update_user(12345, "alice")
        assert db_mod.find_user_by_username("alice") == 12345
