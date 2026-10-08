"""Filesystem layout, and the two ways a layout bug stays invisible."""

from __future__ import annotations

from pathlib import Path

from app import paths


def test_session_path_has_no_suffix() -> None:
    """Kurigram appends `.session` itself, so passing it here would double it up."""
    assert paths.session_path(123).name == "123"


def test_session_file_is_the_path_kurigram_actually_writes() -> None:
    assert paths.session_file(123) == paths.SESSIONS_DIR / "123.session"


def test_test_mode_sessions_are_isolated() -> None:
    """A live test run must never clobber a real user's session."""
    assert paths.session_file(123, test_mode=True) == paths.TEST_SESSIONS_DIR / "123.session"


def test_session_paths_are_load_bearing_strings() -> None:
    """`/login` writes `<id>.session`; a stray dot in the name would break the lookup."""
    assert paths.session_file(123).suffix == ".session"


def test_bot_session_sits_outside_the_per_user_directory() -> None:
    """`/logout` and the reaper operate on SESSIONS_DIR; the bot must not be reachable there."""
    assert paths.BOT_DIR not in paths.SESSIONS_DIR.parents
    assert paths.SESSIONS_DIR not in paths.BOT_DIR.parents


def test_cookie_and_job_paths_are_per_user_and_per_job() -> None:
    assert paths.cookie_path(7) != paths.cookie_path(8)
    assert paths.job_tmp_dir(1) != paths.job_tmp_dir(2)


def test_runtime_directories_can_be_created_any_number_of_times() -> None:
    paths.ensure_runtime_dirs()
    paths.ensure_runtime_dirs()

    for directory in (
        paths.SESSIONS_DIR,
        paths.TEST_SESSIONS_DIR,
        paths.COOKIES_DIR,
        paths.BOT_DIR,
        paths.TMP_DIR,
        paths.DOWNLOADS_DIR,
    ):
        assert directory.is_dir()


# ── permissions on the credential directories (R10) ─────────────────────


def test_credential_directories_are_tightened(monkeypatch) -> None:
    """Plaintext credentials lean on filesystem permissions, so 0700 is the safeguard.

    Without it a default mkdir leaves them world-readable and the bot merely warns about
    it at startup, which is what happened inside the container.
    """
    tightened: list[Path] = []
    monkeypatch.setattr(paths, "_restrict", tightened.append)

    paths.ensure_runtime_dirs()

    assert set(tightened) == set(paths.SECRET_DIRS)


def test_bulk_directories_are_not_treated_as_credentials() -> None:
    """Downloads and temp files are not secrets; restricting them would be theatre."""
    assert paths.TMP_DIR not in paths.SECRET_DIRS
    assert paths.DOWNLOADS_DIR not in paths.SECRET_DIRS


def test_restrict_never_raises(monkeypatch, tmp_path) -> None:
    """A filesystem that refuses chmod must not stop the bot from starting."""
    paths._restrict(tmp_path)  # no-op on Windows, tightens on POSIX

    monkeypatch.setattr(paths.os, "name", "nt")
    paths._restrict(tmp_path)
