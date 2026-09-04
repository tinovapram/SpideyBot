"""Tests for utils.files — filename sanitization."""

import json

from utils.files import build_caption, extract_native_text, extract_post_text, sanitize_filename


class TestBuildCaption:
    def test_no_native(self):
        caption = build_caption("photo.jpg", "https://example.com/x")
        assert caption.startswith("photo.jpg")
        assert "Downloaded by SpideyBot" in caption
        assert "https://example.com/x" in caption

    def test_native_prepended(self):
        caption = build_caption("photo.jpg", "https://x.com", native="A great photo")
        assert caption.startswith("A great photo")
        # native is followed by the filename line
        assert "\n\nphoto.jpg\n\n" in caption
        assert "Downloaded by SpideyBot" in caption

    def test_blank_native_ignored(self):
        caption = build_caption("v.mp4", "https://x.com", native="   ")
        assert caption.startswith("v.mp4")


class TestExtractNativeText:
    def test_twitter_metadata(self, tmp_path):
        path = tmp_path / "metadata.json"
        path.write_text(json.dumps({
            "category": "twitter", "author": "user", "text": "Hello world",
        }), encoding="utf-8")
        assert extract_native_text([str(path)]) == "Hello world"

    def test_reddit_metadata(self, tmp_path):
        path = tmp_path / "metadata.json"
        path.write_text(json.dumps({
            "category": "reddit", "author": "u/a", "title": "Cool title",
            "selftext": "long body",
        }), encoding="utf-8")
        assert extract_native_text([str(path)]) == "Cool title"

    def test_youtube_title(self, tmp_path):
        path = tmp_path / "metadata.json"
        path.write_text(json.dumps({"category": "youtube", "title": "My Video"}), encoding="utf-8")
        assert extract_native_text([str(path)]) == "My Video"

    def test_missing_file(self):
        assert extract_native_text(["/nonexistent/x.json"]) is None


class TestExtractPostText:
    def test_author_category_prefix(self, tmp_path):
        path = tmp_path / "metadata.json"
        path.write_text(json.dumps({
            "category": "twitter", "author": "bob", "text": "hi",
        }), encoding="utf-8")
        assert extract_post_text([str(path)]) == "bob on twitter:\n\nhi\n"
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
