"""Per-user TeraBox ``ndus`` cookie storage, Fernet-encrypted at rest.

A user may paste their own TeraBox cookie; we store only the cookie string,
encrypted with the same Fernet key used for Telegram sessions
(``session_encrypt_key``).

Sharing rule (per user requirement): a **free** user's cookie joins the
shared pool — used to serve other users' TeraBox downloads when the bot's own
accounts are busy.  Pro/premium cookies stay private to their owner.
"""

from __future__ import annotations

from cryptography.fernet import Fernet, InvalidToken

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.config import get_settings
from core.models import User, UserCookie
from core.tiers import DEFAULT_TIER, effective_tier

_cipher: Fernet | None = None


class CookieConfigError(Exception):
    """Raised when the encryption key is missing or malformed."""


def _fernet() -> Fernet:
    global _cipher
    if _cipher is None:
        key = get_settings().session_encrypt_key
        if not key:
            raise CookieConfigError(
                "SPIDEY_SESSION_ENCRYPT_KEY is not set — cannot store cookies."
            )
        try:
            _cipher = Fernet(key.encode() if isinstance(key, str) else key)
        except Exception as exc:  # noqa: BLE001 — bad key should surface clearly
            raise CookieConfigError(f"Invalid encryption key: {exc}") from exc
    return _cipher


def _encrypt(cookie: str) -> str:
    return _fernet().encrypt(cookie.encode()).decode()


def _decrypt(token: str) -> str:
    return _fernet().decrypt(token.encode()).decode()


def validate_cookie(cookie: str) -> str | None:
    """Return an error string when *cookie* is unusable, else ``None``.

    A valid TeraBox cookie must contain a non-empty ``ndus=...`` entry.
    """
    stripped = cookie.strip()
    if not stripped:
        return "Cookie is empty."
    if "ndus=" not in stripped:
        return "Cookie must contain an `ndus=...` value."
    return None


async def save_cookie(
    session: AsyncSession,
    user_id: int,
    cookie: str,
) -> UserCookie:
    """Encrypt and upsert *cookie* for *user_id*."""
    error = validate_cookie(cookie)
    if error is not None:
        raise ValueError(error)

    row = await session.get(UserCookie, user_id)
    token = _encrypt(cookie.strip())
    if row is None:
        row = UserCookie(user_id=user_id, cookie_encrypted=token)
        session.add(row)
    else:
        row.cookie_encrypted = token
    await session.flush()
    return row


async def get_cookie(session: AsyncSession, user_id: int) -> str | None:
    """Return the decrypted cookie for *user_id*, or ``None``.

    Raises :class:`InvalidToken` if the stored ciphertext is corrupt.
    """
    row = await session.get(UserCookie, user_id)
    if row is None:
        return None
    return _decrypt(row.cookie_encrypted)


async def has_cookie(session: AsyncSession, user_id: int) -> bool:
    return await session.get(UserCookie, user_id) is not None


async def delete_cookie(session: AsyncSession, user_id: int) -> bool:
    row = await session.get(UserCookie, user_id)
    if row is None:
        return False
    await session.delete(row)
    await session.flush()
    return True


async def shared_cookies(session: AsyncSession) -> list[str]:
    """Return plaintext cookies of **free-tier** users (the shared pool).

    Cookies from expired tiers are also shared (their effective tier is
    ``free``).  Admin users are excluded so an admin's own cookie is never
    silently pooled.
    """
    rows = await session.execute(select(UserCookie))
    result: list[str] = []
    for cookie_row in rows.scalars():
        user = await session.get(User, cookie_row.user_id)
        if user is None or user.is_admin:
            continue
        if effective_tier(user.tier, user.tier_expiry) != DEFAULT_TIER:
            continue
        try:
            result.append(_decrypt(cookie_row.cookie_encrypted))
        except InvalidToken:
            continue
    return result
