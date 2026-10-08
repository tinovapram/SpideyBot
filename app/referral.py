"""Referral capture (FR-1.2, FR-17.6).

Only *capture* lives here for now: an invite arrives as ``/start <referrer_id>`` and is
recorded as **pending**. Crediting happens on the invitee's first successful download,
which belongs with the download pipeline, so that half waits for it.
"""

from __future__ import annotations

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app import users
from app.models import Referral


def parse_referrer_id(payload: str | None) -> int | None:
    """The referrer id carried by a ``/start`` payload, or ``None`` when unusable.

    An absent, non-numeric or non-positive argument is ignored without error (FR-1.2), so
    this returns ``None`` rather than raising. The payload is the bare user id, because the
    deep link is ``t.me/<bot>?start=<user_id>`` (FR-17.1).
    """
    if payload is None:
        return None

    candidate = payload.strip()
    if not candidate.isdigit():
        return None

    value = int(candidate)
    return value if value > 0 else None


async def record_pending(db: AsyncSession, *, referrer_id: int, invitee_id: int) -> bool:
    """Record a pending referral, first-start-wins. Returns whether one was created.

    Two rules from FR-1.2 are enforced here rather than by the caller:

    * a self-referral records nothing;
    * an invitee who already has a referrer keeps the first one. That is the unique
      constraint on ``invitee_id`` plus ``ON CONFLICT DO NOTHING``, so two ``/start``
      commands racing cannot both create a row.

    A referrer who has never started the bot is ignored rather than inserted: the foreign
    key would otherwise reject the row and, worse, a typed payload would let anyone mint
    user records for arbitrary ids.
    """
    if referrer_id == invitee_id:
        return False

    if await users.fetch(db, referrer_id) is None:
        return False

    statement = (
        pg_insert(Referral)
        .values(referrer_id=referrer_id, invitee_id=invitee_id, status="pending")
        .on_conflict_do_nothing(index_elements=[Referral.invitee_id])
        .returning(Referral.id)
    )
    created = (await db.execute(statement)).scalar_one_or_none() is not None
    if not created:
        return False

    # Mirror it onto the user row, which is what the hot paths read.
    invitee = await users.fetch(db, invitee_id)
    if invitee is not None and invitee.referred_by is None:
        invitee.referred_by = referrer_id
        await db.flush()

    return True
