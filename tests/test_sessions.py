"""Tests for core.sessions — file session paths and legacy JSON handling."""

import json
from pathlib import Path

import pytest

from utils import paths


@pytest.fixture(autouse=True)
def isolated_sessions(tmp_path, monkeypatch):
    """Point the sessions directory at a temp folder for each test."""
    monkeypatch.setattr(paths, "SESSIONS_DIR", tmp_path)
    import core.sessions as sessions_mod
    sessions_mod._active_clients.clear()
    sessions_mod._fernet = None
    monkeypatch.setattr("core.config.SESSION_ENCRYPT_KEY", "test" * 11)
    monkeypatch.setattr("core.config.TG_API_ID", "12345")
    monkeypatch.setattr("core.config.TG_API_HASH", "abcdef0123456789abcdef")
    return sessions_mod


class TestPaths:
    def test_session_file_naming(self, isolated_sessions):
        path = isolated_sessions.session_file(123)
        assert path == paths.SESSIONS_DIR / "123.session"

    def test_legacy_file_naming(self, isolated_sessions):
        path = isolated_sessions.legacy_file(123)
        assert path == paths.SESSIONS_DIR / "123.json"


class TestHasSession:
    def test_none(self, isolated_sessions):
        assert isolated_sessions.has_session(1) is False

    def test_file_session(self, isolated_sessions):
        isolated_sessions.session_file(1).touch()
        assert isolated_sessions.has_session(1) is True

    def test_legacy_json(self, isolated_sessions):
        isolated_sessions.legacy_file(1).write_text(json.dumps({"session": "x"}), encoding="utf-8")
        assert isolated_sessions.has_session(1) is True


class TestLegacyMigration:
    def test_migrate_no_file_returns_false(self, isolated_sessions):
        assert isolated_sessions.migrate_legacy(1) is False

    def test_invalid_legacy_returns_false(self, isolated_sessions):
        isolated_sessions.legacy_file(1).write_text("not json", encoding="utf-8")
        assert isolated_sessions.migrate_legacy(1) is False

    def test_valid_file_session_removes_stale_json(self, isolated_sessions):
        isolated_sessions.session_file(1).write_bytes(b"sqlite")
        isolated_sessions.legacy_file(1).write_text(json.dumps({"session": "legacy"}), encoding="utf-8")
        assert isolated_sessions.migrate_legacy(1) is True
        assert isolated_sessions.legacy_file(1).exists() is False

    def test_build_client_missing_returns_none(self, isolated_sessions):
        assert isolated_sessions.build_client(99) is None

    def test_create_login_client_cleans_stale(self, isolated_sessions):
        isolated_sessions.session_file(5).write_bytes(b"old")
        isolated_sessions.legacy_file(5).write_text(json.dumps({"session": "old"}), encoding="utf-8")
        client = isolated_sessions.create_login_client(5)
        from telethon import TelegramClient

        assert isinstance(client, TelegramClient)
        # Stale legacy JSON must be gone; a fresh (empty) file session is created.
        assert isolated_sessions.legacy_file(5).exists() is False
