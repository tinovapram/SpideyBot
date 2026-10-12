"""The message catalog: spec section 7 copy, and the placeholders inside it."""

from __future__ import annotations

import ast
import dataclasses
import pathlib

import pytest

from app import catalog
from app.config import build_settings
from app.quota import BYTES_PER_MB, Usage, human_bytes
from app.tiers import resolve


@pytest.fixture
def settings(fake_secrets: dict[str, str]):
    return build_settings(**fake_secrets)


# ── session badge (spec 2.2 / FR-13) ────────────────────────────────────


def test_no_session_file_shows_the_red_badge() -> None:
    assert catalog.session_badge(has_session=False, is_live=False) == "🔴 No session"


def test_stored_session_shows_the_saved_badge() -> None:
    badge = catalog.session_badge(has_session=True, is_live=False)

    assert badge == "🟡 Session saved (not running)"


def test_live_client_wins_over_the_stored_file() -> None:
    assert catalog.session_badge(has_session=True, is_live=True) == "🟢 Connected"


def test_live_client_without_a_file_is_still_connected() -> None:
    """Sessions can be in-memory (a session string), so the file is not authoritative."""
    assert catalog.session_badge(has_session=False, is_live=True) == "🟢 Connected"


# ── help ────────────────────────────────────────────────────────────────


def test_help_lists_every_documented_command() -> None:
    text = catalog.help_message()

    for command in (
        "/start",
        "/help",
        "/dl",
        "/dt",
        "/cancel",
        "/status",
        "/account",
        "/quota",
        "/sites",
        "/bypass",
        "/referral",
    ):
        assert command in text


def test_admin_block_is_absent_for_normal_users() -> None:
    text = catalog.help_message(is_admin=False)

    assert "Admin only" not in text
    assert "/stats" not in text


def test_admin_block_appears_for_admins() -> None:
    text = catalog.help_message(is_admin=True)

    assert "**Admin only**" in text
    assert "/addpremium" in text


def test_magic_word_is_interpolated() -> None:
    assert "`WOW2`" in catalog.help_message(magic_word="WOW2")


# ── welcome ─────────────────────────────────────────────────────────────


def test_welcome_composes_the_specs_blocks_in_order(settings) -> None:
    tier = resolve(settings)
    text = catalog.welcome_message("Ada", tier, magic_word="WOW")

    assert catalog.WELCOME in text
    assert "**Essential commands**" in text
    assert "**Notes**" in text
    assert text.endswith("**Welcome, Ada!**")
    assert text.index(catalog.WELCOME) < text.index("**Essential commands**")


def test_free_tier_reminder_is_shown_only_to_free_users(settings) -> None:
    free = catalog.welcome_message("Ada", resolve(settings, tier="free"))
    pro = catalog.welcome_message("Ada", resolve(settings, tier="pro"))

    assert catalog.FREE_TIER_NOTE in free
    assert catalog.FREE_TIER_NOTE not in pro


def test_admin_welcome_shows_admin_commands_and_no_free_reminder(settings) -> None:
    tier = resolve(settings, is_admin=True)
    text = catalog.welcome_message("Ada", tier)

    assert "**Admin only**" in text
    assert catalog.FREE_TIER_NOTE not in text


def test_welcome_reports_the_configured_limit(settings) -> None:
    text = catalog.welcome_message("Ada", resolve(settings, tier="free"))

    assert "Link limit: 500 MB" in text


def test_welcome_reports_the_session_state(settings) -> None:
    text = catalog.welcome_message("Ada", resolve(settings), has_session=True, is_live=True)

    assert f"Sessions: {catalog.CONNECTED}" in text


@pytest.mark.parametrize(
    "build",
    [
        lambda s: catalog.help_message(),
        lambda s: catalog.help_message(is_admin=True),
        lambda s: catalog.welcome_message("Ada", resolve(s)),
        lambda s: catalog.welcome_message("Ada", resolve(s, tier="pro")),
    ],
)
def test_no_placeholder_survives_rendering(settings, build) -> None:
    """A leftover brace means a value was never substituted and the user sees `{tier_label}`."""
    assert "{" not in build(settings)


# ── usage texts (spec 7.2) ──────────────────────────────────────────────


def test_usage_texts_name_their_command() -> None:
    assert "**Usage:** /dl *URL*" in catalog.USAGE_DL
    assert "**Usage:** /dt *Telegram link*" in catalog.USAGE_DT
    assert "**Usage:** /cancel *ID*" in catalog.USAGE_CANCEL


def test_usage_cancel_points_at_status() -> None:
    assert "/status" in catalog.USAGE_CANCEL


def test_usage_dt_mentions_the_session_requirement() -> None:
    assert "/login" in catalog.USAGE_DT


# ── /quota (FR-15, spec 7.6) ────────────────────────────────────────────


@pytest.fixture
def free_user(settings):
    return resolve(settings, tier="free")


def test_quota_reports_tier_usage_and_limits(free_user) -> None:
    text = catalog.quota_message(free_user, Usage(downloads=3))

    assert "**Quota Status**" in text
    assert "Tier: 👤 Free" in text
    assert "Downloads today: 3  — 7 left" in text
    assert "Concurrent: 0/1 slots" in text
    assert "Link limit: 500 MB" in text
    assert "{" not in text


def test_quota_hides_the_bonus_and_queue_lines_when_there_are_none(free_user) -> None:
    """FR-15.2 and FR-15.5 ask for both conditionally, so a quiet user sees no zero rows."""
    text = catalog.quota_message(free_user, Usage())

    assert "bonus" not in text
    assert "Queued" not in text


def test_quota_shows_referral_headroom_separately(free_user) -> None:
    """FR-17.7: the bonus is extra headroom, so both the label and the total move."""
    text = catalog.quota_message(free_user, Usage(downloads=1), bonus=20)

    assert "Downloads today: 1 (+20 bonus)  — 29 left" in text


def test_quota_shows_a_waiting_job(free_user) -> None:
    text = catalog.quota_message(free_user, Usage(), slots_active=1, queued=2)

    assert "Queued: 2 waiting for a slot" in text
    assert "Concurrent: 1/1 slots" in text


def test_quota_reports_bandwidth_against_the_cap(free_user) -> None:
    text = catalog.quota_message(free_user, Usage(bytes_in=BYTES_PER_MB))

    assert "Bandwidth today: 1.0 MB / 1.0 GB" in text


def test_unlimited_dimensions_read_as_infinity(settings) -> None:
    """No shipped tier is uncapped for bandwidth, so the case is built explicitly.

    Spec 13 says 0 means unlimited for a size, and rendering that as a zero-byte allowance
    would tell a user they cannot download at all.
    """
    tier = resolve(settings, tier="premium")
    uncapped = dataclasses.replace(
        tier,
        limits=tier.limits.model_copy(update={"daily_mb": 0, "link_total_mb": 0}),
    )

    text = catalog.quota_message(uncapped, Usage(bytes_in=1234))

    assert "Bandwidth today: 1.2 KB / ∞" in text
    assert "Link limit: ∞" in text


def test_quota_warns_once_the_downloads_are_spent(free_user) -> None:
    text = catalog.quota_message(free_user, Usage(downloads=10))

    assert "Downloads today: 10  — 0 left" in text
    assert catalog.QUOTA_BLOCKED in text


def test_quota_warns_when_the_bandwidth_is_spent(free_user) -> None:
    text = catalog.quota_message(free_user, Usage(downloads=1, bytes_in=1024 * BYTES_PER_MB))

    assert "0 left" not in text
    assert catalog.QUOTA_BLOCKED in text


def test_admin_is_labelled_and_noted(settings) -> None:
    text = catalog.quota_message(resolve(settings, is_admin=True), Usage())

    assert "Tier: 👑 Admin" in text
    assert "admin priority" in text
    assert "Concurrent: 0/3 slots" in text, "admin keeps the premium slot count (spec 2.1)"


def test_a_zero_download_count_is_no_allowance_not_unlimited(settings) -> None:
    """A count and a size read 0 differently: this must agree with reserve_download."""
    no_allowance = dataclasses.replace(
        resolve(settings, tier="free"),
        limits=resolve(settings, tier="free").limits.model_copy(update={"daily_downloads": 0}),
    )

    text = catalog.quota_message(no_allowance, Usage())

    assert "0 left" in text
    assert catalog.QUOTA_BLOCKED in text


@pytest.mark.parametrize(
    ("count", "expected"),
    [
        (0, "0 B"),
        (512, "512 B"),
        (1024, "1.0 KB"),
        (1536, "1.5 KB"),
        (1048576, "1.0 MB"),
        (15 * 1048576, "15 MB"),
        (10 * 1024**3, "10 GB"),
    ],
)
def test_byte_counts_read_the_way_the_cap_is_measured(count: int, expected: str) -> None:
    assert human_bytes(count) == expected


def test_negative_byte_counts_are_clamped() -> None:
    """A counter that went backwards must not render as a negative size."""
    assert human_bytes(-1) == "0 B"


# ── /status and /cancel (FR-11, FR-12, spec 7.4) ────────────────────────


def test_status_with_nothing_running_says_so() -> None:
    assert catalog.status_message([]) == "✅ No active downloads."


def test_status_lists_id_link_and_state() -> None:
    text = catalog.status_message(
        [catalog.JobLine(id=42, link="https://example.test/a", state=catalog.STATE_RUNNING)]
    )

    assert "**Active downloads:**" in text
    assert "  `42` — https://example.test/a — 🔄 Running" in text


def test_status_lists_every_job_in_the_order_given() -> None:
    text = catalog.status_message(
        [
            catalog.JobLine(id=1, link="one", state=catalog.STATE_RUNNING),
            catalog.JobLine(id=2, link="two", state=catalog.STATE_WAITING),
        ]
    )

    assert text.index("`1`") < text.index("`2`")
    assert "⏳ Waiting for a slot" in text


def test_a_waiting_job_is_distinguished_from_a_running_one() -> None:
    """FR-10.6: this distinction is the reason /status exists rather than a job count."""
    assert catalog.job_state(status="queued") == catalog.STATE_WAITING
    assert catalog.job_state(status="running") == catalog.STATE_RUNNING


def test_a_requested_cancellation_reads_as_cancelled() -> None:
    """Otherwise /cancel looks like it did nothing for as long as the worker takes to stop."""
    state = catalog.job_state(status="running", cancel_requested=True)

    assert state == catalog.STATE_CANCELLED


def test_an_unreadable_state_degrades_to_running() -> None:
    """FR-12.3: a job must never be hidden because its state could not be made sense of."""
    assert catalog.job_state(status="something-new") == catalog.STATE_RUNNING


def test_short_links_are_left_whole() -> None:
    assert catalog.truncate_link("https://example.test/a") == "https://example.test/a"


def test_long_links_are_truncated_to_the_limit() -> None:
    link = "https://example.test/" + "x" * 200

    shortened = catalog.truncate_link(link, limit=20)

    assert shortened == "https://example.tes…"
    assert len(shortened) == 20


def test_cancel_confirms_both_an_immediate_and_a_requested_cancellation() -> None:
    assert catalog.cancel_message(kind="cancelled", job_id=7) == "❌ Cancelled download **7**."
    assert catalog.cancel_message(kind="requested", job_id=7) == "❌ Cancelled download **7**."


def test_cancel_reports_an_unknown_or_finished_job_as_no_active_download() -> None:
    """A delivered job is not "cancelled": there is simply nothing left to cancel (FR-11.3)."""
    for kind in ("unknown", "finished"):
        assert catalog.cancel_message(kind=kind, job_id=7) == ("⚠️ No active download with ID `7`.")


def test_cancel_refuses_someone_elses_job_without_naming_the_owner() -> None:
    assert catalog.cancel_message(kind="not_owned", job_id=7) == (
        "⚠️ That download belongs to another user."
    )


def test_cancel_usage_and_invalid_id_copy_exist() -> None:
    assert "/cancel *ID*" in catalog.USAGE_CANCEL
    assert "/status" in catalog.CANCEL_INVALID_ID


# ── failure reporting (FR-9, spec 7.7) ──────────────────────────────────


def test_nothing_failed_is_an_empty_block() -> None:
    """The completion message must not grow a `0 file(s) failed:` header."""
    assert catalog.failure_block([]) == ""


def test_failures_are_counted_then_listed() -> None:
    block = catalog.failure_block(
        [catalog.Failure(name="clip.mp4", reason="404"), catalog.Failure("song.mp3", "timeout")]
    )

    assert block.splitlines()[0] == "⚠️ 2 file(s) failed:"
    assert "• clip.mp4 — 404" in block
    assert "• song.mp3 — timeout" in block


def test_a_failure_with_a_url_is_hyperlinked() -> None:
    """FR-9.3: the name is linked when a source URL is known, so the user can retry it."""
    block = catalog.failure_block(
        [catalog.Failure(name="clip.mp4", reason="404", url="https://host/x")]
    )

    assert "• [clip.mp4](https://host/x) — 404" in block


def test_brackets_in_a_filename_cannot_break_the_link() -> None:
    """Filenames come from the internet, and an unescaped `]` would end the link early."""
    block = catalog.failure_block(
        [catalog.Failure(name="a]b[c.mp4", reason="404", url="https://host/x")]
    )

    assert "• [a\\]b\\[c.mp4](https://host/x) — 404" in block


def test_the_list_is_capped_and_the_remainder_summarised() -> None:
    """A share where everything fails must still produce a message Telegram accepts."""
    failures = [catalog.Failure(name=f"file{i}.mp4", reason="404") for i in range(25)]

    block = catalog.failure_block(failures, limit=10)

    assert block.count("•") == 11, "ten bullets plus the remainder line"
    assert "• …and 15 more" in block
    assert "⚠️ 25 file(s) failed:" in block.splitlines()[0]


def test_exactly_the_limit_failures_produce_no_remainder_line() -> None:
    failures = [catalog.Failure(name=f"f{i}", reason="x") for i in range(10)]

    block = catalog.failure_block(failures, limit=10)

    assert "…and" not in block


def test_the_zero_byte_reason_is_the_spec_wording() -> None:
    assert catalog.EMPTY_FILE_REASON == "empty file (0 bytes)"


# ── lifecycle copy (spec 7.3) ───────────────────────────────────────────


@pytest.mark.parametrize(
    "message",
    [
        catalog.NO_FILES_SENT,
        catalog.EMPTY_SHARE,
        catalog.DOWNLOAD_CANCELLED,
        catalog.FALLBACK_NOTICE,
        catalog.ERROR_OCCURRED,
        catalog.DONE_WITH_FILES,
    ],
)
def test_lifecycle_copy_matches_the_spec(message: str) -> None:
    assert message.startswith(("✅", "❌", "ℹ️", "⚠️"))


def test_the_error_message_carries_the_reason() -> None:
    """E1: a user sees why, not a bare 'an error occurred'."""
    assert "boom" in catalog.ERROR_OCCURRED.format(error="boom")


def test_the_done_message_reports_counts_and_size() -> None:
    line = catalog.DONE_WITH_FILES.format(sent=7, total=10, size="512")

    assert line == "✅ **SpideyBot: Done!** Sent 7/10 files (512 MB)."


def test_no_catalogue_name_is_defined_twice() -> None:
    """A second assignment silently replaces the first, and the user sees the wrong string.

    This is not hypothetical: the live status message's header was added as ``STATUS_HEADER``, which
    is also `/status`'s listing header, so AC-6 started rendering the job's phase instead. The test
    that caught it compares a rendered string; this one says *why*, and catches the next one before
    it reaches a user.
    """
    source = pathlib.Path(catalog.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    assigned: list[str] = [
        target.id
        for node in tree.body
        if isinstance(node, ast.Assign)
        for target in node.targets
        if isinstance(target, ast.Name)
    ]
    duplicates = sorted({name for name in assigned if assigned.count(name) > 1})

    assert duplicates == [], f"defined more than once: {duplicates}"


def test_no_announced_constant_is_also_an_annotated_one() -> None:
    """``X = ...`` and ``X: str = ...`` are two definitions too, and easier to miss."""
    source = pathlib.Path(catalog.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    assigned = {
        target.id
        for node in tree.body
        if isinstance(node, ast.Assign)
        for target in node.targets
        if isinstance(target, ast.Name)
    }
    annotated = {
        node.target.id
        for node in tree.body
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name)
    }

    assert assigned & annotated == set()
