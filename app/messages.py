"""Final message construction for completed jobs (FR-7.9, FR-9.4).

This module owns the wording of the replacement message that replaces the
progress message on completion. It is separate from catalog.py so that
telegram.status can import it without creating a circular import with app.jobs.
"""

from __future__ import annotations

from dataclasses import dataclass

from app import catalog
from downloader.flow import LinkResult


@dataclass(frozen=True)
class JobDialogue:
    """The messages a finished job produces, before anything is sent.

    Returning the text rather than sending it keeps the wording decisions testable on their own, and
    keeps this module from being the second place that knows catalog's format strings.
    """

    body: str
    delivered: int
    failed: int


def final_message(outcome: LinkResult, *, total: int) -> JobDialogue:
    """FR-7.9's replacement message, in the shapes the spec fixes.

    The failure block is appended rather than sent separately (FR-9.4), so a job with 17 successes
    and 3 failures stays one message.
    """
    delivered = len(outcome.files)
    failed = len(outcome.failures)
    block = catalog.failure_block(list(outcome.failures))

    if outcome.refused is not None:
        body = catalog.limit_exceeded_message(
            actual_mb=outcome.refused.actual_mb, limit_mb=outcome.refused.limit_mb
        )
        return JobDialogue(body=body, delivered=0, failed=0)

    if outcome.cancelled:
        return JobDialogue(body=catalog.DOWNLOAD_CANCELLED, delivered=0, failed=failed)

    if outcome.empty_share:
        return JobDialogue(body=catalog.EMPTY_SHARE, delivered=0, failed=0)

    if delivered == 0:
        reason = outcome.reason or catalog.NO_FILES_SENT
        body = catalog.ERROR_OCCURRED.format(error=reason)
        return JobDialogue(
            body=f"{block}\n{body}" if block else body, delivered=0, failed=failed
        )

    body = catalog.DONE_WITH_FILES.format(
        sent=delivered, total=max(total, delivered), size=_mb(outcome.bytes_written)
    )
    return JobDialogue(
        body=f"{body}\n{block}" if block else body, delivered=delivered, failed=failed
    )


def _mb(count: int) -> str:
    """Whole megabytes for the done message, which shows ``{size} MB`` (spec §7.6)."""
    return f"{count / (1024 * 1024):.1f}".rstrip("0").rstrip(".")