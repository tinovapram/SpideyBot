"""
Tests for spideybot.config — URL detection, size limits, concurrency limits.
"""

import os
import pytest


class TestIsTeraboxUrl:
    """Tests for is_terabox_url()."""

    def test_standard_terabox(self):
        from spideybot.config import is_terabox_url
        assert is_terabox_url("https://terabox.com/s/abc123") is True

    def test_nephobox(self):
        from spideybot.config import is_terabox_url
        assert is_terabox_url("https://nephobox.com/s/xyz") is True

    def test_dubox(self):
        from spideybot.config import is_terabox_url
        assert is_terabox_url("https://www.dubox.com/s/123") is True

    def test_1024tera(self):
        from spideybot.config import is_terabox_url
        assert is_terabox_url("https://1024tera.com/s/abc") is True

    def test_teraboxapp(self):
        from spideybot.config import is_terabox_url
        assert is_terabox_url("https://teraboxapp.com/s/abc") is True

    def test_case_insensitive(self):
        from spideybot.config import is_terabox_url
        assert is_terabox_url("https://TERABOX.COM/s/ABC") is True

    def test_non_terabox_url(self):
        from spideybot.config import is_terabox_url
        assert is_terabox_url("https://youtube.com/watch?v=abc") is False
        assert is_terabox_url("https://reddit.com/r/pics") is False
        assert is_terabox_url("https://twitter.com/user/status/123") is False

    def test_empty_string(self):
        from spideybot.config import is_terabox_url
        assert is_terabox_url("") is False

    def test_partial_domain_match(self):
        """Ensure 'terabox' substring in unrelated domain still matches (intended behavior)."""
        from spideybot.config import is_terabox_url
        # This matches because "terabox" appears in the URL string
        assert is_terabox_url("https://notteraboxexample.com/terabox") is True


class TestGetSizeLimit:
    """Tests for get_size_limit()."""

    def test_admin_unlimited(self):
        from spideybot.config import get_size_limit
        size, label = get_size_limit(is_premium=False, is_admin=True)
        assert size == float("inf")
        assert label == "Unlimited"

    def test_premium(self):
        from spideybot.config import get_size_limit, SIZE_LIMIT_PREMIUM
        size, label = get_size_limit(is_premium=True, is_admin=False)
        assert size == SIZE_LIMIT_PREMIUM
        assert isinstance(label, str)
        assert "MB" in label or "GB" in label

    def test_free(self):
        from spideybot.config import get_size_limit, SIZE_LIMIT_FREE
        size, label = get_size_limit(is_premium=False, is_admin=False)
        assert size == SIZE_LIMIT_FREE
        assert isinstance(label, str)


class TestGetConcurrentLimit:
    """Tests for get_concurrent_limit()."""

    def test_premium_limit(self):
        from spideybot.config import get_concurrent_limit, CONCURRENT_LIMIT_PREMIUM
        assert get_concurrent_limit(is_premium=True) == CONCURRENT_LIMIT_PREMIUM

    def test_free_limit(self):
        from spideybot.config import get_concurrent_limit, CONCURRENT_LIMIT_FREE
        assert get_concurrent_limit(is_premium=False) == CONCURRENT_LIMIT_FREE
