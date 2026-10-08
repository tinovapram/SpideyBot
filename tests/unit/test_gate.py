"""The pre-transfer size gate (E3, AC-3): refuse what is too big, and how the numbers read."""

from __future__ import annotations

import pytest

from app import catalog
from downloader.gate import check_size, format_mb

MB = 1024 * 1024


def test_a_share_within_the_limit_is_allowed() -> None:
    verdict = check_size(100 * MB, limit_mb=500)

    assert verdict.allowed is True
    assert verdict.enforce_during_transfer is False


def test_a_share_exactly_at_the_limit_is_allowed() -> None:
    """The spec refuses what *exceeds* the limit, so the boundary itself passes."""
    assert check_size(500 * MB, limit_mb=500).allowed is True


def test_a_share_over_the_limit_is_refused() -> None:
    verdict = check_size(700 * MB, limit_mb=500)

    assert verdict.allowed is False
    assert verdict.actual_mb == "700"
    assert verdict.limit_mb == "500"


def test_an_unlimited_tier_never_refuses() -> None:
    """0 means unlimited throughout the tier table (spec 13)."""
    verdict = check_size(50 * 1024 * MB, limit_mb=0)

    assert verdict.allowed is True
    assert verdict.unlimited is True
    assert verdict.limit_bytes is None


def test_an_unknown_size_passes_the_gate_but_is_flagged_for_enforcement() -> None:
    """Neither refuse nor shrug: an extractor that reports no size is common, but the cap holds."""
    verdict = check_size(None, limit_mb=500)

    assert verdict.allowed is True
    assert verdict.enforce_during_transfer is True
    assert verdict.limit_bytes == 500 * MB


def test_an_unlimited_tier_asks_for_no_enforcement() -> None:
    """There is no cap to enforce, so there is nothing for the fetcher to watch."""
    verdict = check_size(None, limit_mb=0)

    assert verdict.allowed is True
    assert verdict.enforce_during_transfer is False


def test_a_zero_byte_share_is_not_refused_here() -> None:
    """An empty share is reported by the failure path (E6), not by the size gate."""
    verdict = check_size(0, limit_mb=500)

    assert verdict.allowed is True
    assert verdict.actual_bytes == 0


@pytest.mark.parametrize(
    ("total_bytes", "expected"),
    [
        (0, "0"),
        (MB, "1"),
        (int(1.5 * MB), "1.5"),
        (500 * MB, "500"),
        (1024 * MB, "1024"),
        (int(0.5 * MB), "0.5"),
    ],
)
def test_megabytes_read_the_way_the_copy_expects(total_bytes: int, expected: str) -> None:
    """The spec's sentence is `{actual} MB`, so a trailing `.0` would read as sloppy copy."""
    assert format_mb(total_bytes) == expected


def test_a_tiny_size_renders_as_zero_rather_than_inventing_a_digit() -> None:
    """52 KB is 0.049 MB. Showing `0.1` would be a made-up digit; the gate refuses nothing here."""
    assert format_mb(52428) == "0"


def test_negative_input_does_not_render_as_a_negative_size() -> None:
    assert format_mb(-1) == "0"


def test_the_refusal_reads_as_the_spec_writes_it() -> None:
    """AC-3 wants both numbers, before transfer; this is the sentence the user gets."""
    verdict = check_size(700 * MB, limit_mb=500)

    message = catalog.limit_exceeded_message(actual_mb=verdict.actual_mb, limit_mb=verdict.limit_mb)

    assert message == (
        "⚠️ **SpideyBot: Limit Exceeded.** Total share size is `700 MB`, "
        "exceeding your limit of `500`."
    )
