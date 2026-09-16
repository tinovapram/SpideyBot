"""Referral system: code generation, deep-link parsing, reward granting.

Link format: ``https://t.me/<bot_username>?start=<referrer_id>``

Telegram delivers the payload as ``/start <referrer_id>`` to the bot.
Self-referral is rejected.  An invitee can only have one referrer
(``UNIQUE(invitee_id)``).  The reward is credited on the invitee's **first
successful download**, not on join.

The bonus is **read-computed** — :func:`bonus_downloads` counts credited
referrals within the bonus window and multiplies by the per-referral bonus.
No daily re-grant job is needed; the quota path simply adds this headroom.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from core.config import get_settings
from core.models import Referral

# ── Link / payload helpers ──────────────────────────────────────


def referral_link(user_id: int, bot_username: str) -> str:
    """Build the deep-link for *user_id* using the bot username."""
    return f"https://t.me/{bot_username}?start={user_id}"


def parse_start_payload(text: str) -> int | None:
    """Extract the referrer user id from a ``/start <code>`` message.

    Returns ``None`` when the payload is absent or not a positive integer.
    """
    parts = text.split(maxsplit=1)
    if len(parts) < 2:
        return None
    payload = parts[1].strip()
    if not payload.isdigit():
        return None
    referrer_id = int(payload)
    if referrer_id <= 0:
        return None
    return referrer_id


# ── Recording ──────────────────────────────────────────────────


async def record_referral(
    session: AsyncSession,
    *,
    referrer_id: int,
    invitee_id: int,
) -> Referral | None:
    """Insert a pending referral row.

    Returns the :class:`Referral` on success, or ``None`` when:
    - self-referral (referrer == invitee)
    - invitee already has a referrer (first-start-wins)
    """
    if referrer_id == invitee_id:
        return None

    existing = await session.scalar(
        select(Referral).where(Referral.invitee_id == invitee_id)
    )
    if existing is not None:
        return None

    referral = Referral(
        referrer_id=referrer_id,
        invitee_id=invitee_id,
        status="pending",
    )
    session.add(referral)
    await session.flush()
    return referral


# ── Reward granting ─────────────────────────────────────────────


async def try_credit_on_download(
    session: AsyncSession,
    invitee_id: int,
) -> bool:
    """Credit the referrer when the invitee completes their first download.

    Idempotent: if the referral is already ``credited`` or doesn't exist,
    returns ``False``.  On success, stamps ``credited_at`` so the bonus
    (read-computed in :func:`bonus_downloads`) starts applying.
    """
    referral = await session.scalar(
        select(Referral).where(Referral.invitee_id == invitee_id)
    )
    if referral is None or referral.status != "pending":
        return False

    referral.status = "credited"
    referral.credited_at = datetime.now(timezone.utc)
    return True


async def bonus_downloads(session: AsyncSession, user_id: int) -> int:
    """Extra daily downloads a referrer is entitled to, read-computed.

    Each credited referral still within the bonus window contributes
    ``referral_daily_bonus`` extra daily downloads.  No daily re-grant job
    is needed — the quota path adds this headroom when checking limits.
    """
    settings = get_settings()
    cutoff = datetime.now(timezone.utc) - timedelta(days=settings.referral_bonus_days)

    count = await session.scalar(
        select(func.count())
        .select_from(Referral)
        .where(
            Referral.referrer_id == user_id,
            Referral.status == "credited",
            Referral.credited_at >= cutoff,
        )
    ) or 0
    return count * settings.referral_daily_bonus


# ── Stats ───────────────────────────────────────────────────────


async def referral_stats(session: AsyncSession, user_id: int, *, bot_username: str) -> dict:
    """Return referral statistics for *user_id*."""
    total_count = await session.scalar(
        select(func.count()).select_from(Referral).where(Referral.referrer_id == user_id)
    ) or 0
    credited_count = await session.scalar(
        select(func.count())
        .select_from(Referral)
        .where(Referral.referrer_id == user_id, Referral.status == "credited")
    ) or 0
    pending_count = await session.scalar(
        select(func.count())
        .select_from(Referral)
        .where(Referral.referrer_id == user_id, Referral.status == "pending")
    ) or 0

    return {
        "link": referral_link(user_id, bot_username),
        "total": total_count,
        "credited": credited_count,
        "pending": pending_count,
    }
