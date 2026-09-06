"""Migrate V3 SQLite user data to V4 PostgreSQL.

Usage:
    python scripts/migrate_v3_to_v4.py [path/to/v3.db]

Defaults to ``data/bot_database.db`` (standard V3 location).
Requires ``SPIDEY_DATABASE_URL`` env var (or .env) for the V4 target.
"""

from __future__ import annotations

import asyncio
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

# Add project root to path so core.* imports work
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import text
from core.db import engine, init_db


def read_v3_users(db_path: str) -> list[dict]:
    """Read all users from V3 SQLite database."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute("SELECT * FROM users").fetchall()
        return [dict(r) for r in rows]
    except sqlite3.OperationalError as exc:
        print(f"Error reading V3 database: {exc}")
        print("Make sure the path points to a valid V3 bot_database.db")
        sys.exit(1)
    finally:
        conn.close()


def transform(user: dict) -> dict:
    """Convert V3 user row to V4 format."""
    now = datetime.now(timezone.utc)

    # V3 premium_expiry is a unix timestamp (int), V4 tier_expiry is datetime
    expiry_ts = user.get("premium_expiry", 0) or 0
    tier_expiry = (
        datetime.fromtimestamp(expiry_ts, tz=timezone.utc)
        if expiry_ts > 0
        else None
    )

    return {
        "id": user["user_id"],
        "username": user.get("username"),
        "tier": "pro" if user.get("is_premium") else "free",
        "tier_expiry": tier_expiry,
        "is_admin": False,
        "created_at": now,
        "updated_at": now,
    }


async def migrate(db_path: str) -> None:
    """Run the migration."""
    # 1. Read V3 data
    users = read_v3_users(db_path)
    if not users:
        print("No V3 users found. Nothing to migrate.")
        return

    print(f"Found {len(users)} V3 users in {db_path}")

    # 2. Init V4 database (creates tables if needed)
    init_db()

    # 3. Upsert into V4 PostgreSQL
    inserted = 0
    updated = 0
    skipped = 0

    async with engine().begin() as conn:
        for u in users:
            v4 = transform(u)

            # Check if user already exists
            result = await conn.execute(
                text("SELECT id FROM users WHERE id = :id"), {"id": v4["id"]}
            )
            existing = result.fetchone()

            if existing:
                # Update username and tier if changed
                await conn.execute(
                    text("""
                        UPDATE users
                        SET username = :username,
                            tier = :tier,
                            tier_expiry = :tier_expiry,
                            updated_at = :updated_at
                        WHERE id = :id
                    """),
                    v4,
                )
                updated += 1
            else:
                await conn.execute(
                    text("""
                        INSERT INTO users (id, username, tier, tier_expiry, is_admin, created_at, updated_at)
                        VALUES (:id, :username, :tier, :tier_expiry, :is_admin, :created_at, :updated_at)
                    """),
                    v4,
                )
                inserted += 1

    print(f"Migration complete: {inserted} inserted, {updated} updated, {skipped} skipped")


def main() -> None:
    db_path = sys.argv[1] if len(sys.argv) > 1 else "data/bot_database.db"

    if not Path(db_path).exists():
        print(f"V3 database not found: {db_path}")
        print("Usage: python scripts/migrate_v3_to_v4.py [path/to/v3.db]")
        sys.exit(1)

    asyncio.run(migrate(db_path))


if __name__ == "__main__":
    main()
