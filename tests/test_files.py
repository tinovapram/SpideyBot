"""Tests for utils.files — filename sanitization."""

from utils.files import sanitize_filename


class TestSanitizeFilename:
    def test_normal(self):
        assert sanitize_filename("photo.jpg") == "photo.jpg"

    def test_windows_invalid_chars(self):
        result = sanitize_filename('file<>:"/\\|?*.txt')
        for char in '<>:"/\\|?*':
            assert char not in result
        assert result.endswith(".txt")

    def test_leading_trailing_spaces(self):
        result = sanitize_filename("  hello  .txt")
        assert not result.startswith(" ")
        assert not result.endswith(" ")
        assert "hello" in result

    def test_long_name_truncated(self):
        result = sanitize_filename(f"{'a' * 200}.png")
        assert len(result.rsplit(".", 1)[0]) <= 120
        assert result.endswith(".png")

    def test_empty_name(self):
        assert "file" in sanitize_filename("....")

    def test_long_extension(self):
        result = sanitize_filename("name." + "x" * 20)
        assert len(result.rsplit(".", 1)[1]) <= 10

    def test_no_extension(self):
        assert sanitize_filename("Makefile") == "Makefile"

    def test_control_characters(self):
        result = sanitize_filename("file\x00name.txt")
        assert "\x00" not in result
        assert result.endswith(".txt")
