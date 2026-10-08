"""Filesystem layout, and the two ways a layout bug stays invisible."""

from __future__ import annotations

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
