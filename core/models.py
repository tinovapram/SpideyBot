"""SQLAlchemy ORM models (async, PostgreSQL)."""

from __future__ import annotations

from datetime import date, datetime
from uuid import UUID

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
    Uuid,
    func,
)
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from core.config import is_admin as _cfg_is_admin
from core.tiers import DEFAULT_TIER


class Base(DeclarativeBase):
    """Declarative base for all ORM models."""


class User(Base):
    """Telegram user record — tier, username, admin flag."""

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    username: Mapped[str | None] = mapped_column(String, nullable=True)
    tier: Mapped[str] = mapped_column(String(20), nullable=False, default="free")
    tier_expiry: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    is_admin: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    timer_media_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    magic_word: Mapped[str] = mapped_column(String(50), nullable=False, default="WOW")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class QuotaUsage(Base):
    """Per-user, per-period quota counters (atomic upsert target)."""

    __tablename__ = "quota_usage"

    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"), primary_key=True)
    period_start: Mapped[date] = mapped_column(Date, primary_key=True)
    period_type: Mapped[str] = mapped_column(String(10), primary_key=True)  # daily | monthly
    downloads: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    bytes_down: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    bytes_up: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)


class DownloadJob(Base):
    """A queued download — the persistent priority queue."""

    __tablename__ = "download_jobs"
    __table_args__ = (Index("idx_jobs_claim", "status", "priority", "created_at"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"), index=True)
    link: Mapped[str] = mapped_column(Text, nullable=False)
    site: Mapped[str | None] = mapped_column(String(40), nullable=True)
    tier: Mapped[str] = mapped_column(String(20), nullable=False, default="free")
    priority: Mapped[float] = mapped_column(Float, nullable=False, default=2.0)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="queued", index=True)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    total_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    claim_token: Mapped[UUID | None] = mapped_column(Uuid, nullable=True)
    lease_expires: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class DownloadHistory(Base):
    """User-facing download history (also drives /history and retention)."""

    __tablename__ = "download_history"
    __table_args__ = (Index("idx_history_user", "user_id", "created_at"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"), index=True)
    job_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("download_jobs.id"), nullable=True
    )
    link: Mapped[str] = mapped_column(Text, nullable=False)
    site: Mapped[str | None] = mapped_column(String(40), nullable=True)
    filename: Mapped[str | None] = mapped_column(Text, nullable=True)
    bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="done")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class Referral(Base):
    """Referral relationship: referrer ← invitee, credited on first download."""

    __tablename__ = "referrals"
    __table_args__ = (UniqueConstraint("invitee_id", name="uq_referrals_invitee"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    referrer_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"), index=True)
    invitee_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"))
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    # pending | credited | revoked
    credited_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

class UserCookie(Base):
    """A user's own TeraBox ``ndus`` cookie, Fernet-encrypted at rest.

    Free-tier cookies join the shared pool (used by other users when the
    bot's own accounts are busy); pro/premium cookies stay private to their
    owner.  The sharing rule is derived from the user's tier at read time,
    not stored here.
    """

    __tablename__ = "user_cookies"

    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id"), primary_key=True
    )
    cookie_encrypted: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


# ── Query helpers ──────────────────────────────────────────────

async def get_or_create_user(
    session: AsyncSession, user_id: int, username: str | None = None
) -> User:
    """Fetch a user by id, creating a free-tier row if absent.

    Updates *username* if the user already exists and the name changed.
    Syncs ``is_admin`` from the config allowlist (single source of truth).
    """
    user = await session.get(User, user_id)
    if user is None:
        user = User(id=user_id, username=username, tier=DEFAULT_TIER, is_admin=_cfg_is_admin(user_id))
        session.add(user)
        await session.flush()
    else:
        # Keep DB admin flag in sync with config allowlist.
        if user.is_admin != _cfg_is_admin(user_id):
            user.is_admin = _cfg_is_admin(user_id)
        if username and user.username != username:
            user.username = username
    return user
