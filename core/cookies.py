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


_MIN_NDUS_LENGTH = 16


def parse_ndus(cookie: str) -> str | None:
    """Return the ``ndus`` value from *cookie*, or ``None`` when absent/empty."""
    for part in cookie.split(";"):
        name, _, value = part.strip().partition("=")
        if name.strip().lower() == "ndus":
            value = value.strip()
            return value or None
    return None


def validate_cookie(cookie: str) -> str | None:
    """Return an error string when *cookie* is unusable, else ``None``.

    A usable TeraBox cookie carries a non-empty, plausibly sized ``ndus=...``.
    This is a format check only — a stale ``ndus`` can still be rejected by
    TeraBox at download time.
    """
    stripped = cookie.strip()
    if not stripped:
        return "Cookie is empty."
    value = parse_ndus(stripped)
    if value is None:
        return "Cookie must contain a non-empty `ndus=...` value."
    if len(value) < _MIN_NDUS_LENGTH:
        return "That `ndus` value looks truncated — copy the whole cookie again."
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


async def has_valid_cookie(session: AsyncSession, user_id: int) -> bool:
    """True when *user_id* stored a cookie that still passes validation.

    Used as the TeraBox access gate: a cookie that cannot be decrypted or no
    longer parses must behave like no cookie at all.
    """
    try:
        cookie = await get_cookie(session, user_id)
    except Exception:  # noqa: BLE001 — any read failure must gate the user
        return False
    return bool(cookie) and validate_cookie(cookie) is None


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
    # Single join: the previous per-row ``session.get(User, ...)`` turned this
    # into an N+1 query on every TeraBox job.
    rows = await session.execute(
        select(UserCookie.cookie_encrypted, User.tier, User.tier_expiry)
        .join(User, User.id == UserCookie.user_id)
        .where(User.is_admin.is_(False))
    )
    result: list[str] = []
    for token, tier, tier_expiry in rows:
        if effective_tier(tier, tier_expiry) != DEFAULT_TIER:
            continue
        try:
            result.append(_decrypt(token))
        except InvalidToken:
            continue
    return result
