"""Tests for multi-account (multi-ndus) TeraBox configuration & pool logic."""

from __future__ import annotations

import pytest

from core import config
from downloader.terabox import TeraBoxAccountPool, TeraBoxDownloader, TeraBoxAuthError


class TestConfigCookies:
    def test_split_pure(self):
        assert config._split_terabox_cookies("ndus=AAA | ndus=BBB") == ["ndus=AAA", "ndus=BBB"]
        assert config._split_terabox_cookies("a|b|c") == ["a", "b", "c"]
        assert config._split_terabox_cookies("") == []
        assert config._split_terabox_cookies("   |  |") == []

    def test_multi_wins_over_legacy(self, monkeypatch):
        monkeypatch.setattr(config, "TERABOX_COOKIES", "ndus=AAA|ndus=BBB")
        monkeypatch.setattr(config, "TERABOX_COOKIE", "ndus=OLD")
        assert config.terabox_account_cookies() == ["ndus=AAA", "ndus=BBB"]

    def test_fallback_to_legacy_single(self, monkeypatch):
        monkeypatch.setattr(config, "TERABOX_COOKIES", "")
        monkeypatch.setattr(config, "TERABOX_COOKIE", "ndus=SINGLE")
        assert config.terabox_account_cookies() == ["ndus=SINGLE"]

    def test_empty(self, monkeypatch):
        monkeypatch.setattr(config, "TERABOX_COOKIES", "")
        monkeypatch.setattr(config, "TERABOX_COOKIE", None)
        assert config.terabox_account_cookies() == []


class TestPool:
    @staticmethod
    def _make_downloader(suffix: str) -> TeraBoxDownloader:
        return TeraBoxDownloader(cookie=f"ndus={suffix}")

    def test_round_robin_order(self):
        d1 = self._make_downloader("a" * 32)
        d2 = self._make_downloader("b" * 32)
        d3 = self._make_downloader("c" * 32)
        pool = TeraBoxAccountPool([d1, d2, d3])

        assert len(pool) == 3
        assert [a is d1 for a in pool.ordered_accounts()] == [True, False, False]
        assert pool.ordered_accounts()[0] is d2
        assert pool.ordered_accounts()[0] is d3
        # wraps back around
        assert pool.ordered_accounts()[0] is d1

    def test_every_cycle_returns_all_accounts(self):
        accounts = [self._make_downloader(f"{c}" * 32) for c in "xyz"]
        pool = TeraBoxAccountPool(accounts)
        for _ in range(3):
            order = pool.ordered_accounts()
            assert {id(a) for a in order} == {id(a) for a in accounts}
            assert len(order) == 3

    def test_rejects_empty(self):
        with pytest.raises(TeraBoxAuthError):
            TeraBoxAccountPool([])
