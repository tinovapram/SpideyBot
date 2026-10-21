from __future__ import annotations

from pathlib import Path

import pytest

from utils import paths


def test_clear_staging_removes_download_dirs_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(paths, "DOWNLOADS_DIR", tmp_path)

    (tmp_path / "123456").mkdir()
    (tmp_path / "123456" / "file.mp4").write_text("partial", encoding="utf-8")
    (tmp_path / "tg_42").mkdir()
    (tmp_path / "tg_42" / "photo.jpg").write_text("partial", encoding="utf-8")
    (tmp_path / "notes.txt").write_text("operator file", encoding="utf-8")

    paths.clear_staging()

    assert not (tmp_path / "123456").exists()
    assert not (tmp_path / "tg_42").exists()
    assert (tmp_path / "notes.txt").read_text(encoding="utf-8") == "operator file"
    assert tmp_path.is_dir()
