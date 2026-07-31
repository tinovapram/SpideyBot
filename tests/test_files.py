"""
Tests for spideybot.utils.files — filename sanitization.
"""

import pytest
from spideybot.utils.files import sanitize_filename


class TestSanitizeFilename:
    """Tests for sanitize_filename()."""

    def test_normal_filename(self):
        assert sanitize_filename("photo.jpg") == "photo.jpg"

    def test_windows_invalid_chars(self):
        result = sanitize_filename('file<>:"/\\|?*.txt')
        assert "<" not in result
        assert ">" not in result
        assert ":" not in result
        assert '"' not in result
        assert "/" not in result
        assert "\\" not in result
        assert "|" not in result
        assert "?" not in result
        assert "*" not in result
        assert result.endswith(".txt")

    def test_leading_trailing_spaces(self):
        result = sanitize_filename("  hello  .txt")
        assert not result.startswith(" ")
        assert not result.endswith(" ")
        assert "hello" in result

    def test_leading_periods(self):
        result = sanitize_filename("...hidden.jpg")
        assert not result.startswith(".")
        assert result.endswith(".jpg")

    def test_long_filename_truncated(self):
        long_name = "a" * 200
        result = sanitize_filename(f"{long_name}.png")
        name_part = result.rsplit(".", 1)[0]
        assert len(name_part) <= 120
        assert result.endswith(".png")

    def test_empty_name_becomes_file(self):
        result = sanitize_filename("....")
        assert "file" in result

    def test_long_extension_truncated(self):
        result = sanitize_filename("name." + "x" * 20)
        ext_part = result.rsplit(".", 1)[1] if "." in result else ""
        assert len(ext_part) <= 10

    def test_custom_max_len(self):
        result = sanitize_filename("abcdefghij.txt", max_len=5)
        name_part = result.rsplit(".", 1)[0]
        assert len(name_part) <= 5

    def test_no_extension(self):
        result = sanitize_filename("Makefile")
        assert result == "Makefile"

    def test_control_characters(self):
        result = sanitize_filename("file\x00name.txt")
        assert "\x00" not in result
        assert result.endswith(".txt")
