"""SQLAlchemy models for SpideyBot."""

from __future__ import annotations

from sqlalchemy import Boolean, Column, Integer, String, create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from utils import paths


class Base(DeclarativeBase):
    """Declarative base for all ORM models."""


class User(Base):
    """Telegram user record — tier, username, premium status."""

    __tablename__ = "users"

    user_id = Column(Integer, primary_key=True)
    username = Column(String, nullable=True)
    is_premium = Column(Boolean, default=False)
    premium_expiry = Column(Integer, default=0)

    def __repr__(self) -> str:
        return (
            f"<User user_id={self.user_id} username={self.username!r} "
            f"premium={self.is_premium}>"
        )


_engine = None
_SessionLocal = None


def init_models() -> None:
    """Create engine, tables and session factory. Called once at startup."""
    global _engine, _SessionLocal

    paths.ensure_directories()

    _engine = create_engine(
        f"sqlite:///{paths.DB_PATH}",
        echo=False,
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(_engine)
    _SessionLocal = sessionmaker(bind=_engine, expire_on_commit=False)


def get_session():
    """Return a new SQLAlchemy session (caller is responsible for closing it)."""
    if _SessionLocal is None:
        raise RuntimeError("init_models() must be called before get_session()")
    return _SessionLocal()
