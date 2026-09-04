"""Tests for core.config — URL detection and tier limits."""

import pytest

from core import config


class TestIsTeraboxUrl:
    @pytest.mark.parametrize(
        "url",
        [
            "https://terabox.com/s/abc123",
            "https://nephobox.com/s/xyz",
            "https://www.dubox.com/s/123",
            "https://1024tera.com/s/abc",
            "https://teraboxapp.com/s/abc",
            "https://TERABOX.COM/s/ABC",
        ],
    )
    def test_terabox_domains(self, url):
        assert config.is_terabox_url(url) is True

    @pytest.mark.parametrize(
        "url",
        [
            "https://youtube.com/watch?v=abc",
            "https://reddit.com/r/pics",
            "https://twitter.com/user/status/123",
            "",
        ],
    )
    def test_non_terabox(self, url):
        assert config.is_terabox_url(url) is False


class TestGetSizeLimit:
    def test_admin_unlimited(self):
        size, label = config.get_size_limit(is_premium=False, is_admin=True)
        assert size == float("inf")
        assert label == "Unlimited"

    def test_premium(self):
        size, label = config.get_size_limit(is_premium=True, is_admin=False)
        assert size == config.SIZE_LIMIT_PREMIUM
        assert "MB" in label or "GB" in label

    def test_free(self):
        size, _ = config.get_size_limit(is_premium=False, is_admin=False)
        assert size == config.SIZE_LIMIT_FREE


class TestGetConcurrentLimit:
    def test_premium(self):
        assert config.get_concurrent_limit(is_premium=True) == config.CONCURRENT_LIMIT_PREMIUM

    def test_free(self):
        assert config.get_concurrent_limit(is_premium=False) == config.CONCURRENT_LIMIT_FREE
