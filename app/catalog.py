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


# ── the download lifecycle (FR-3, FR-9, spec 7.3 and 7.7) ───────────────

QUEUING_DOWNLOAD = (
    "⏳ **SpideyBot: Queuing download…**\n"
    "  ID: `{id}`\n"
    "  Link: {link}\n"
    "  Position: {position}"
)

LIMIT_EXCEEDED = (
    "⚠️ **SpideyBot: Limit Exceeded.** Total share size is `{actual} MB`, "
    "exceeding your limit of `{limit}`."
)
FAILURE_HEADER = "⚠️ {count} file(s) failed:"
FAILURE_BULLET = "• {name} — {reason}"
FAILURE_MORE = "• …and {remaining} more"
NO_FILES_SENT = "❌ **SpideyBot:** No files were successfully sent."
EMPTY_SHARE = "ℹ️ **SpideyBot:** No actual files found in this share."
ERROR_OCCURRED = "❌ **SpideyBot: An error occurred.**\nError: `{error}`"
DOWNLOAD_CANCELLED = "❌ **SpideyBot:** Download cancelled."
FALLBACK_NOTICE = "⚠️ **SpideyBot:** Primary download failed, trying {engine}"
DONE_WITH_FILES = "✅ **SpideyBot: Done!** Sent {sent}/{total} files ({size} MB)."

#: FR-9.5's exact wording, so the reason is never paraphrased differently in two places.
EMPTY_FILE_REASON = "empty file (0 bytes)"

#: FR-8.3 asks each delivered file to carry a caption and to include the source link "where useful",
#: leaving the wording to us (unlike the messages §7 fixes verbatim).
SOURCE_CAPTION = "🔗 {source}"

#: The reason for a share stopped mid-flight. Distinct from LIMIT_EXCEEDED because that one states
#: a size the extractor measured, while this one can only say where the transfer was stopped (D31).
LIMIT_EXCEEDED_REASON = "passed the {limit} MB limit while downloading"

#: FR-7.3's phases, in the order they happen. Delivery adds the last two.
PHASE_RESOLVING = "Resolving"
PHASE_DOWNLOADING = "Downloading"
PHASE_UPLOADING = "Uploading"
PHASE_DONE = "Done"

# ── the live status message (FR-7) ──────────────────────────────────────
# The layout belongs to telegram/status.py; the sentences belong here, like every other string the
# user reads (D23). FR-7.2 fixes three zones: a header, rows, and a persistent footer.
#
# Prefixed ``JOB_`` on purpose: ``STATUS_*`` above already means `/status`'s listing, and a second
# ``STATUS_HEADER`` would have silently replaced it — which it did, once, and AC-6 caught it.

JOB_HEADER = "🔄 **SpideyBot: {phase}{ellipsis}**"
JOB_ID_LINE = "  ID: `{id}`"
JOB_LINK_LINE = "  Link: {link}"
#: FR-7.4's "current transfer byte progress", with the total when the host reported one.
JOB_TRANSFER = "  {name}\n  {bar} {percent}% ({done}/{total})"
JOB_TRANSFER_UNSIZED = "  {name}\n  {done} transferred"
JOB_FILES_DOWNLOADED = "  Downloaded: {done}/{total} files"
#: FR-7.5's per-file upload context.
JOB_UPLOADING_FILE = "  Uploading (file {index}/{total})"
JOB_FILES_UPLOADED = "  Uploaded: {done}/{total} files"
JOB_FILES_SENT = "  Sent: {done}/{total} files"
JOB_FILES_FAILED = "  Failed: {count} file(s)"
#: E2's fallback notice, shown in the message rather than only in the log: the user's link is being
#: retried by a different engine, and that is the reason the job looks like it restarted.
JOB_NOTICE = "  ⚠️ {notice}"
#: FR-7.2's persistent footer. Present in every phase, including the failure shapes, because the
#: moment a user wants to cancel is the moment the job looks stuck.
JOB_FOOTER = "  '/cancel {id}' to stop"


@dataclass(frozen=True)
class Failure:
    """One file that did not make it, and why (FR-9.2)."""

    name: str
    reason: str
    url: str | None = None


def _escape_markdown(text: str) -> str:
    """Filenames come from the internet, so brackets would otherwise break the link syntax."""
    return text.replace("[", "\\[").replace("]", "\\]")


def failure_block(failures: Sequence[Failure], *, limit: int = 10) -> str:
    """The failure list, or an empty string when nothing failed (FR-9.3, FR-9.4).

    Capped at ``limit`` bullets followed by a remainder count, so a share in which everything fails
    still produces a message Telegram will accept rather than one it truncates.
    """
    if not failures:
        return ""

    lines = [FAILURE_HEADER.format(count=len(failures))]
    for failure in failures[:limit]:
        name = _escape_markdown(failure.name)
        shown = f"[{name}]({failure.url})" if failure.url else name
        lines.append(FAILURE_BULLET.format(name=shown, reason=failure.reason))

    remaining = len(failures) - limit
    if remaining > 0:
        lines.append(FAILURE_MORE.format(remaining=remaining))
    return "\n".join(lines)


def limit_exceeded_message(*, actual_mb: str, limit_mb: str) -> str:
    """The refusal AC-3 asks for: both numbers, before anything was transferred."""
    return LIMIT_EXCEEDED.format(actual=actual_mb, limit=limit_mb)


def fallback_notice(engine: str) -> str:
    """E2's notice while the next resolver is tried (spec §7.3).

    The spec's sentence names gallery-dl, and that is exactly what it renders for the gallery-dl
    step; naming the engine is what keeps the same sentence truthful for the others in the chain
    (D6), rather than announcing gallery-dl while a native adapter runs.
    """
    return FALLBACK_NOTICE.format(engine=engine)


# ── accounts and sessions (FR-13, spec 7.6) ─────────────────────────────
#
# The login copy is the spec's, verbatim where it exists; the rest is written here because §7.6
# names the error cases (E13/E14/E15) without wording them. One deliberate edit: the spec's login
# warning says the session is stored as an "encrypted file session", and nothing is encrypted (D9),
# so the sentence says what actually happens instead.

LOGIN_PHONE = (
    "🔐 **Login to your Telegram account**\n\n"
    "This lets SpideyBot access private/restricted content on your behalf.\n\n"
    "⚠️ Your session is stored as a local file session.\n"
    "You can revoke access at any time with /logout.\n\n"
    "Please send your **phone number** (e.g. `+1234567890`):"
)
LOGIN_CODE = "📩 A code was sent to `{masked}`.\n\nPlease send the **code** you received:"
LOGIN_PASSWORD = "🔒 Two-factor authentication is enabled.\n\nPlease send your **2FA password**:"
LOGIN_SUCCESS = (
    "✅ **Login successful!**\n\n"
    "Logged in as `{masked}`.\n\n"
    "Use `/stop` to disconnect, or `/logout` to revoke."
)
LOGIN_ALREADY = (
    "✅ You are already logged in.\nUse /logout first to sign in with a different account."
)
LOGIN_IN_PROGRESS = "⏳ A login is already in progress. Send /cancel to abort it."
LOGIN_NONE = "ℹ️ There is no login in progress."
LOGIN_CANCELLED = "❌ Login cancelled."
LOGIN_TIMED_OUT = "⌛ The login timed out. Send /login to start again."
LOGIN_FAILED = "❌ Login failed. Send /login to try again."
LOGIN_PHONE_INVALID = (
    "❌ That does not look like a phone number.\n"
    "Send it in international format, e.g. `+1234567890`."
)
#: E13: invalid and expired are separate sentences because only one of them is worth retrying.
LOGIN_CODE_INVALID = "❌ That code is not valid. Send it again, or /cancel to abort."
LOGIN_CODE_EXPIRED = "❌ That code has expired. Send /login to get a new one."
#: E15.
LOGIN_PASSWORD_INVALID = "❌ That password is not correct. Send it again, or /cancel to abort."
#: E14.
LOGIN_FLOOD = "⏳ Telegram is rate limiting this login. Try again in {seconds} seconds."

LOGOUT_DONE = "🔓 Logged out successfully. Your session has been removed."
LOGOUT_NONE = "ℹ️ You are not currently logged in."
#: D10 revokes server-side *and* deletes locally. When the first half fails the user is told,
#: because the session still exists in their Active Sessions list — a fact they cannot see here.
LOGOUT_UNREVOKED = (
    "🔓 The session was removed from SpideyBot, but Telegram could not be told to revoke it "
    "(`{reason}`). Remove it from Settings → Devices if you want it gone there too."
)

ACCOUNT_STATUS = (
    "**Account Status**\n\n"
    "{badge}\n\n"
    "Use /login to connect your Telegram account, or /logout to revoke it."
)
#: FR-13.1: the badge is the whole answer, so the line that names the alternative is added only when
#: there is no session to describe.
ACCOUNT_HINT = "TeraBox cookies are managed with /setting."

# ── the Telegram download path (FR-4) ───────────────────────────────────
DT_NOT_TELEGRAM = "⚠️ /dt takes Telegram links only. Use /dl for any other site."
DT_NO_SESSION = (
    "⚠️ You need a Telegram session before downloading from Telegram.\n"
    "Send /login to connect your account, then send the link again."
)
DT_SESSION_EXPIRED = "⚠️ Your session is no longer valid ({reason})\nSend /login to connect again."
DT_MESSAGE_MISSING = (
    "❌ Message `{id}` was not found in `{chat}`.\n"
    "It may be deleted, or your account may not have access to it."
)
DT_CHAT_UNREACHABLE = "❌ Could not open `{chat}`: {reason}"
DT_NO_MEDIA = "ℹ️ That message has no media to download."
DT_RANGE_EMPTY = "ℹ️ No media found in messages {start}-{end} of `{chat}`."
DT_FOLDER_EMPTY = "ℹ️ Nothing downloadable was found in that folder."
#: FR-4.5: files *and* what was scanned, plus the source chat's title. The scanned count is what
#: distinguishes "the range was empty" from "the range had no media in it".
DT_DONE = "✅ **Sent {sent}/{total} file(s)** from `{chat}` — scanned {scanned} message(s)."
DT_DONE_ALBUM = "✅ **Sent {sent}/{total} file(s)** from `{chat}` (one album)."
DT_CAPTION_FALLBACK = "🔗 From {chat}, message {id}"

# ── the reply magic word (FR-5) and auto-save (FR-6) ────────────────────
#
# Both speak only in Saved Messages: the command may have been typed in someone else's chat, and a
# bot that answered there — as the user — would be doing something the user did not ask for.

#: FR-5.4: no reply to act on. The word is named because the user may have changed it (FR-14).
MAGIC_NO_REPLY = (
    "⚠️ **Reply to a message that has media, then send `{word}`** to save it here."
)
MAGIC_NO_MEDIA = (
    "⚠️ **That message has no media to save.**\n"
    "Reply to one with a photo, video or file, then send `{word}`."
)
MAGIC_SAVED = "✅ **SpideyBot:** Saved the media from that message."
MAGIC_FAILED = "❌ **SpideyBot:** Could not save that media (`{reason}`)."

#: FR-6.3: who sent it, which account, and what they wrote.
TIMER_CAPTION = "📥 Saved from {username} (`{sender_id}`)\n\n{text}"
TIMER_SAVED = "✅ **SpideyBot:** Saved an incoming private photo/video."
