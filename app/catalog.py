"""User-facing copy, kept in one place (spec section 7).

The spec says implementation may adjust wording but must preserve meaning, emoji
semantics and information content — so these are the spec's strings, verbatim, and
the only edits allowed are the ones that substitute a live value for a `{placeholder}`.

Keeping the copy here rather than inline in handlers means the wording can be
reviewed as a whole, and a test can assert the placeholders that must survive.
"""

from __future__ import annotations

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
