"""
SpideyBot — SQLAlchemy Models.

Defines all database tables via ORM for clean, readable database access.
"""

import os

from sqlalchemy import (
    Column,
    Integer,
    String,
    Boolean,
    create_engine,
)
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from spideybot import db


class Base(DeclarativeBase):
    """SQLAlchemy declarative base."""


class User(Base):
    """Telegram user record — tier, username, and premium status."""

    __tablename__ = "users"

    user_id = Column(Integer, primary_key=True)
    username = Column(String, nullable=True)
    is_premium = Column(Boolean, default=False)
    premium_expiry = Column(Integer, default=0)

    def __repr__(self):
        return (
            f"<User user_id={self.user_id} "
            f"username={self.username!r} premium={self.is_premium}>"
        )


# ─── Engine / Session Factory ──────────────────────────────────────

_engine = None
_SessionLocal = None


def init_models() -> None:
    """Create engine, tables, and session factory. Called once at startup."""
    global _engine, _SessionLocal
    os.makedirs(os.path.dirname(db.DB_PATH), exist_ok=True)
    _engine = create_engine(f"sqlite:///{db.DB_PATH}", echo=False)
    Base.metadata.create_all(_engine)
    _SessionLocal = sessionmaker(bind=_engine)


def get_session():
    """Return a new SQLAlchemy session (caller must close it)."""
    if _SessionLocal is None:
        raise RuntimeError("Call init_models() before using get_session()")
    return _SessionLocal()
