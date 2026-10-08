"""User-facing copy, kept in one place (spec section 7).

The spec says implementation may adjust wording but must preserve meaning, emoji
semantics and information content — so these are the spec's strings, verbatim, and
the only edits allowed are the ones that substitute a live value for a `{placeholder}`.

Keeping the copy here rather than inline in handlers means the wording can be
reviewed as a whole, and a test can assert the placeholders that must survive.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from app.queue import QUEUED, CancelKind
from app.quota import BYTES_PER_MB, Usage, bandwidth_exhausted, human_bytes
from app.tiers import EffectiveTier, format_limit_mb

# Spec 2.2 / FR-13: the three states a session can be in.
CONNECTED = "🟢 Connected"
SESSION_SAVED = "🟡 Session saved (not running)"
NO_SESSION = "🔴 No session"


def session_badge(*, has_session: bool, is_live: bool) -> str:
    """Pick the badge for the session's state.

    A live client wins over a stored file: while the client is running the file is
    incidental, and reporting "not running" would contradict the connection the user
    is actually using.
    """
    if is_live:
        return CONNECTED
    return SESSION_SAVED if has_session else NO_SESSION


WELCOME = (
    "**Welcome to SpideyBot**\n"
    "Download videos, files, and media from 30+ platforms — including Terabox, "
    "YouTube, Reddit, Instagram, and more."
)

TIER_BLOCK = """    Tier: {tier_label}
    Link limit: {limit_label}
    Sessions: {session_badge}"""

ESSENTIAL_COMMANDS = """**Essential commands**

    /start  — Sign up or refresh your status
    /help   — Show this help guide
    /dl *URL*  — Download a file
    /dt *t.me link*  — Download from a Telegram message
    /cancel *ID*  — Cancel a queued download
    /status — View your active downloads
    /account — Account & session status
    /quota — View your usage & limits
    /sites — List supported platforms
    /bypass *URL* — Resolve link shorteners to final destination
    /referral — Get your invite link & stats"""

# The magic word is interpolated because an operator can change it in config.
# Split across lines with implicit concatenation rather than reflowed: the rendered
# text is spec copy, so a stray newline would change what the user reads.
NOTES = (
    "**Notes**\n"
    "\n"
    "    • Your tier sets how many downloads run at once (1/2/3) — extra links wait in the "
    "queue. See /quota.\n"
    "    • A finished job replaces the progress message with the result; files that\n"
    "      failed to download or upload are listed there by name.\n"
    "    • Terabox links require a logged-in account — use /account to manage sessions.\n"
    "    • Reply to media and send your magic word (default: `{magic_word}`) to download it.\n"
    "    • Private photos/videos are auto-saved — manage via /setting."
)

ADMIN_ONLY = """**Admin only**

    /stats  — View live download statistics
    /addpremium — Grant Pro to a user
    /removepremium — Revoke Pro from a user
    /checkpremium — Check a user's premium status"""

FREE_TIER_NOTE = "🆓 Free tier: 1 download at a time, size limit per download."

WELCOME_NAMED = "**Welcome, {first_name}!**"

ALREADY_STARTED = "ℹ️ **SpideyBot:** Already started — your session is active."

USAGE_DL = "**Usage:** /dl *URL*\n\nProvide a link to download."

USAGE_DT = (
    "**Usage:** /dt *Telegram link*\n\n"
    "Download directly from a Telegram message (t.me link). "
    "Requires an active session — use /start or /login."
)

USAGE_CANCEL = "**Usage:** /cancel *ID*\n\nView your active downloads with /status."


def tier_block(tier: EffectiveTier, *, has_session: bool, is_live: bool) -> str:
    return TIER_BLOCK.format(
        tier_label=tier.label,
        limit_label=format_limit_mb(tier.limits.link_total_mb),
        session_badge=session_badge(has_session=has_session, is_live=is_live),
    )


def already_started_message(*, has_session: bool, is_live: bool) -> str:
    """The short repeat-`/start` notice (FR-1.5).

    Brief by requirement — it must not re-send the onboarding content — but it still
    reports the session state, so a user whose session was just reconnected can see that
    (FR-1.5, AC-1).
    """
    badge = session_badge(has_session=has_session, is_live=is_live)
    return f"{ALREADY_STARTED}\n\n    Sessions: {badge}"


def help_message(*, magic_word: str = "WOW", is_admin: bool = False) -> str:
    """The `/help` body: commands, then notes, then the admin block when it applies."""
    blocks = [ESSENTIAL_COMMANDS, NOTES.format(magic_word=magic_word)]
    if is_admin:
        blocks.append(ADMIN_ONLY)
    return "\n\n".join(blocks)


def welcome_message(
    first_name: str,
    tier: EffectiveTier,
    *,
    magic_word: str = "WOW",
    has_session: bool = False,
    is_live: bool = False,
) -> str:
    """The full onboarding body, composed from the spec's blocks.

    Order follows spec 7.1 and AC-1: welcome, live status, commands, notes, and the
    free-tier reminder only when it is actually true of this user.
    """
    blocks = [
        WELCOME,
        tier_block(tier, has_session=has_session, is_live=is_live),
        help_message(magic_word=magic_word, is_admin=tier.is_admin),
    ]
    if tier.name == "free" and not tier.is_admin:
        blocks.append(FREE_TIER_NOTE)
    blocks.append(WELCOME_NAMED.format(first_name=first_name))
    return "\n\n".join(blocks)


# ── /quota (FR-15, spec 7.6) ────────────────────────────────────────────
#
# Note the deliberate asymmetry: 0 means *unlimited* for a size (spec 13), but for a download
# count it means no allowance at all -- which is what app.quota.reserve_download enforces. The
# two are rendered differently for that reason, and neither is a special case of the other.

QUOTA_TEMPLATE = """**Quota Status**

  Tier: {tier_label}
  Downloads today: {downloads}{bonus}  — {remaining}
  Bandwidth today: {bandwidth}
  Concurrent: {active}/{total} slots{slot_note}
  Link limit: {link_limit}{queued}"""

QUOTA_BONUS = " (+{bonus} bonus)"
QUOTA_QUEUED = "\n  Queued: {count} waiting for a slot"
QUOTA_BLOCKED = "⚠️ **Quota reached** — try again tomorrow."
QUOTA_UNLIMITED = "∞"


def _bandwidth_line(usage: Usage, daily_mb: int) -> str:
    """``used / cap``, or ``used / ∞`` when the tier has no bandwidth cap."""
    used = human_bytes(usage.bytes_in)
    if daily_mb <= 0:
        return f"{used} / {QUOTA_UNLIMITED}"
    return f"{used} / {human_bytes(daily_mb * BYTES_PER_MB)}"


def quota_message(
    tier: EffectiveTier,
    usage: Usage,
    *,
    bonus: int = 0,
    slots_active: int = 0,
    queued: int = 0,
) -> str:
    """The `/quota` body (FR-15).

    ``bonus`` is extra daily headroom from referral credits, shown only when there is some, and
    ``queued`` only when a job is actually waiting — both per FR-15.2 and FR-15.5, which ask for
    those lines conditionally so a user with neither is not shown a row of zeros.
    """
    limits = tier.limits
    allowance = limits.daily_downloads + bonus
    remaining = max(0, allowance - usage.downloads)

    note = " (extra links wait in the queue)"
    if tier.is_admin:
        note = " (admin priority; extra links wait in the queue)"

    body = QUOTA_TEMPLATE.format(
        tier_label=tier.label,
        downloads=usage.downloads,
        bonus=QUOTA_BONUS.format(bonus=bonus) if bonus > 0 else "",
        remaining=f"{remaining} left",
        bandwidth=_bandwidth_line(usage, limits.daily_mb),
        active=slots_active,
        total=limits.concurrent,
        slot_note=note,
        link_limit=format_limit_mb(limits.link_total_mb),
        queued=QUOTA_QUEUED.format(count=queued) if queued > 0 else "",
    )

    if remaining == 0 or bandwidth_exhausted(usage, limits.daily_mb):
        return f"{body}\n\n{QUOTA_BLOCKED}"
    return body


# ── /status and /cancel (FR-11, FR-12, spec 7.4) ────────────────────────

STATUS_EMPTY = "✅ No active downloads."
STATUS_HEADER = "**Active downloads:**"
STATUS_LINE = "  `{id}` — {link} — {state}"

STATE_RUNNING = "🔄 Running"
STATE_WAITING = "⏳ Waiting for a slot"
STATE_CANCELLED = "❌ Cancelled"

CANCEL_INVALID_ID = "⚠️ **Invalid ID** — pass the number shown by /status."
CANCEL_UNKNOWN = "⚠️ No active download with ID `{id}`."
CANCEL_NOT_OWNED = "⚠️ That download belongs to another user."
CANCEL_CONFIRMED = "❌ Cancelled download **{id}**."

#: Long links would push the state off the edge of a phone screen, and the id and state are
#: what the user came for.
LINK_DISPLAY_LIMIT = 60


def truncate_link(link: str, *, limit: int = LINK_DISPLAY_LIMIT) -> str:
    """A link short enough to keep a status line readable."""
    if len(link) <= limit:
        return link
    return f"{link[: limit - 1]}…"


def job_state(*, status: str, cancel_requested: bool = False) -> str:
    """The state a user sees for one job (FR-12.2).

    A cancellation that has been *requested* already reads as cancelled. The user asked for it
    and the worker is winding the job down, so reporting "Running" for those seconds would look
    like `/cancel` had done nothing. Anything unrecognised degrades to Running rather than
    raising: FR-12.3 asks that a job never be hidden because its state could not be read.
    """
    if cancel_requested:
        return STATE_CANCELLED
    if status == QUEUED:
        return STATE_WAITING
    return STATE_RUNNING


@dataclass(frozen=True)
class JobLine:
    """One row of `/status`, reduced to the three things it shows."""

    id: int
    link: str
    state: str


def status_message(jobs: Sequence[JobLine]) -> str:
    """The `/status` body: nothing, or one line per live job (FR-12.1, FR-12.2)."""
    if not jobs:
        return STATUS_EMPTY
    rows = "\n".join(STATUS_LINE.format(id=job.id, link=job.link, state=job.state) for job in jobs)
    return f"{STATUS_HEADER}\n\n{rows}"


def cancel_message(*, kind: CancelKind, job_id: int) -> str:
    """What `/cancel` answers, given what the queue decided (FR-11.3, FR-11.4, FR-11.5).

    A job that already finished is reported as unknown, because that is what it is from here:
    there is no active download with that id to cancel.
    """
    if kind in ("cancelled", "requested"):
        return CANCEL_CONFIRMED.format(id=job_id)
    if kind == "not_owned":
        return CANCEL_NOT_OWNED
    return CANCEL_UNKNOWN.format(id=job_id)
