"""ORM models.

Everything except credentials lives here. Sessions and cookies are plaintext files
(D7-D9), so there is deliberately **no** ``user_secrets`` table.

Two shape decisions carry real weight:

* ``job_items`` doubles as the idempotency ledger (FR-8.5, E26). ``files_sent`` is
  derived from it, so a job resumed after a restart cannot re-upload what already
  reached the user — the "exactly once" property the spec insists on.
* A job stores its own status message id, so a restarted worker rebinds to the same
  message rather than posting a second one (FR-7.1, E7).
"""

from __future__ import annotations

from datetime import UTC, date, datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), default=utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=utcnow, default=utcnow
    )


class User(TimestampMixin, Base):
    """A Telegram user. ``id`` is the Telegram id, not a surrogate key."""

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    username: Mapped[str | None] = mapped_column(String(64))
    first_name: Mapped[str | None] = mapped_column(String(128))

    tier: Mapped[str] = mapped_column(String(16), default="free", server_default="free")
    tier_expiry: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")

    # FR-1.5/1.6: "already started" persists across restarts; NULL means onboarding
    # has not completed, which is what decides full welcome vs short notice.
    onboarded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    referred_by: Mapped[int | None] = mapped_column(BigInteger)

    grants: Mapped[list[TierGrant]] = relationship(back_populates="user")


class TierGrant(Base):
    """Grants are rows, not a boolean, because they carry an expiry (FR-2.2, FR-19)."""

    __tablename__ = "tier_grants"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    tier: Mapped[str] = mapped_column(String(16))
    granted_by: Mapped[int | None] = mapped_column(BigInteger)
    granted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, server_default=func.now()
    )
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    user: Mapped[User] = relationship(back_populates="grants")


class UsageDaily(Base):
    """One row per user-day, so quota updates are a single atomic upsert (FR-3.2)."""

    __tablename__ = "usage_daily"

    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    day: Mapped[date] = mapped_column(Date, primary_key=True)

    downloads: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    bonus_downloads: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    bytes_in: Mapped[int] = mapped_column(BigInteger, default=0, server_default="0")
    bytes_out: Mapped[int] = mapped_column(BigInteger, default=0, server_default="0")


class Job(TimestampMixin, Base):
    __tablename__ = "jobs"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    link: Mapped[str] = mapped_column(Text)
    site: Mapped[str | None] = mapped_column(String(64))
    tier: Mapped[str] = mapped_column(String(16))
    priority: Mapped[float] = mapped_column(Float, default=0.0, server_default="0")

    # queued | running | delivered | failed | cancelled (spec 8.1)
    status: Mapped[str] = mapped_column(String(16), default="queued", server_default="queued")
    route: Mapped[str | None] = mapped_column(String(16))
    attempts: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    total_bytes: Mapped[int | None] = mapped_column(BigInteger)
    resolved_count: Mapped[int | None] = mapped_column(Integer)

    # Lease, so a crashed worker's job is re-queued rather than lost (FR-10.5, E26).
    claim_token: Mapped[str | None] = mapped_column(PgUUID(as_uuid=False))
    lease_expires: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # The single status message this job owns (FR-7.1).
    status_chat_id: Mapped[int | None] = mapped_column(BigInteger)
    status_message_id: Mapped[int | None] = mapped_column(BigInteger)

    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancel_requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    items: Mapped[list[JobItem]] = relationship(
        back_populates="job", cascade="all, delete-orphan", order_by="JobItem.idx"
    )

    __table_args__ = (
        # Drives the claim query: claimable status, best priority, oldest first.
        Index("ix_jobs_claim", "status", "priority", "created_at"),
    )


class JobItem(Base):
    """One file within a job. Also the idempotency ledger (see module docstring).

    ``source_url`` is the *resolved, fetchable* per-file URL, never the link the user pasted
    (that lives on ``Job.link``). One pasted album link therefore yields one row per file, and
    the ``(job_id, source_url)`` constraint means a resumed job can never upload the same
    resolved media twice.
    """

    __tablename__ = "job_items"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    job_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("jobs.id", ondelete="CASCADE"), index=True
    )
    idx: Mapped[int] = mapped_column(Integer)
    source_url: Mapped[str] = mapped_column(Text)
    filename: Mapped[str | None] = mapped_column(Text)
    media_kind: Mapped[str | None] = mapped_column(String(16))
    bytes_total: Mapped[int | None] = mapped_column(BigInteger)
    bytes_done: Mapped[int] = mapped_column(BigInteger, default=0, server_default="0")

    # pending | downloaded | uploaded | failed
    state: Mapped[str] = mapped_column(String(16), default="pending", server_default="pending")
    reason: Mapped[str | None] = mapped_column(Text)
    uploaded_message_id: Mapped[int | None] = mapped_column(BigInteger)

    job: Mapped[Job] = relationship(back_populates="items")

    __table_args__ = (
        UniqueConstraint("job_id", "idx", name="uq_job_items_job_idx"),
        # Without this, a resumed job could queue the same URL twice (FR-8.5).
        UniqueConstraint("job_id", "source_url", name="uq_job_items_job_url"),
    )


class Referral(Base):
    """One referrer per invitee, first-start-wins (FR-17.6, E22)."""

    __tablename__ = "referrals"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    referrer_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    invitee_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id", ondelete="CASCADE"))
    # pending | credited | revoked. Credited only on the invitee's first successful
    # download (FR-17.4), never on join.
    status: Mapped[str] = mapped_column(String(16), default="pending", server_default="pending")
    credited_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, server_default=func.now()
    )

    __table_args__ = (UniqueConstraint("invitee_id", name="uq_referrals_invitee"),)


class ReferralBonus(Base):
    """+10 downloads/day per credit, each valid for 30 days (FR-17.5)."""

    __tablename__ = "referral_bonuses"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    referrer_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    invitee_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id", ondelete="CASCADE"))
    credited_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, server_default=func.now()
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class UserPref(Base):
    """Per-user behaviour. NULL means "unset", which is not the same as "off".

    ``timer_media`` in particular must distinguish unset from disabled, because an
    unreadable preference has to fail *open* (FR-6.4, E18).
    """

    __tablename__ = "user_prefs"

    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    magic_word: Mapped[str | None] = mapped_column(String(64))
    timer_media: Mapped[bool | None] = mapped_column(Boolean)
    terabox_ndus: Mapped[str | None] = mapped_column(String(256))
    locale: Mapped[str | None] = mapped_column(String(8))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, server_default=func.now()
    )


class StatsDaily(Base):
    """Counters behind the admin /stats view (FR-19)."""

    __tablename__ = "stats_daily"

    day: Mapped[date] = mapped_column(Date, primary_key=True)
    submitted: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    delivered: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    failed: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    cancelled: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    files_sent: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    bytes_up: Mapped[int] = mapped_column(BigInteger, default=0, server_default="0")
