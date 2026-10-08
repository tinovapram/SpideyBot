"""The pre-transfer size gate (E3, AC-3).

AC-3 requires an oversized share to be refused **before** a single byte is transferred, naming both
the actual size and the limit. That is why resolution reports sizes at all (D30) and why this
decision is made on what the extractor reported rather than on what we managed to download: the
point is to spend neither bandwidth nor the user's daily quota discovering something the metadata
already said.

The awkward case is a size nobody reported. Many extractors do not publish one, so refusing would
break links that would have worked; treating "unknown" as "small enough" would let an oversized
share through. The answer is neither: an unknown size passes the gate but comes back **flagged for
enforcement during transfer**, so the fetcher aborts if the running total passes the cap. The rule
is the same either way — the limit holds — but it is enforced where it can be.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.quota import BYTES_PER_MB


def format_mb(total_bytes: int) -> str:
    """A byte count in MB, the way the spec's copy asks for it: ``500``, not ``500.0``.

    Uses the same MB as the rest of the bot (``BYTES_PER_MB``), because these strings sit next to
    ``link_total_mb`` and a second, slightly different MB would make the refusal contradict the
    number the user was given.
    """
    rendered = f"{max(total_bytes, 0) / BYTES_PER_MB:.1f}"
    return rendered.rstrip("0").rstrip(".") if rendered.endswith("0") else rendered


@dataclass(frozen=True)
class SizeCheck:
    """The verdict, and enough information to explain it in the spec's words."""

    allowed: bool
    actual_bytes: int | None
    limit_bytes: int | None
    #: True when the size is unknown and a cap exists, so the fetcher must watch the running total
    #: and abort if it passes the limit. A refusal would be wrong (extractors that report no size
    #: are common) and silence would be worse.
    enforce_during_transfer: bool = False

    @property
    def unlimited(self) -> bool:
        return self.limit_bytes is None

    @property
    def actual_mb(self) -> str:
        return format_mb(self.actual_bytes or 0)

    @property
    def limit_mb(self) -> str:
        return format_mb(self.limit_bytes or 0)


def check_size(actual_bytes: int | None, *, limit_mb: int) -> SizeCheck:
    """Decide whether a share may be downloaded, given what resolution reported.

    ``limit_mb <= 0`` means unlimited throughout the tier table (spec 13), so it always passes.
    A size equal to the limit passes: the spec's wording refuses what *exceeds* it.
    """
    if limit_mb <= 0:
        return SizeCheck(allowed=True, actual_bytes=actual_bytes, limit_bytes=None)

    limit_bytes = limit_mb * BYTES_PER_MB
    if actual_bytes is None:
        return SizeCheck(
            allowed=True,
            actual_bytes=None,
            limit_bytes=limit_bytes,
            enforce_during_transfer=True,
        )
    return SizeCheck(
        allowed=actual_bytes <= limit_bytes,
        actual_bytes=actual_bytes,
        limit_bytes=limit_bytes,
    )
