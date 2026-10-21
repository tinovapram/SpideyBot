# Operations Runbook

## Startup and readiness

The container is ready only after PostgreSQL connectivity, scheduler startup,
worker startup, and Telegram startup all succeed. The bot then writes
`data/ready`; Docker checks that the recorded process is still alive. A stale
marker is removed at startup and shutdown.

## Database migrations

Run migrations before starting the bot:

```powershell
alembic upgrade head
```

The migration history preserves the original schema, while the current head
removes obsolete persistent download tables. User, quota, referral, cookie,
and session-related data are retained.

## Backups and restore

Back up PostgreSQL with `pg_dump` before applying migrations or changing quota
policy. Test restores into a separate database before production recovery.
Do not include `.env`, `data/`, or `user_sessions/` in public artifacts.

## Secret rotation

Rotate Telegram API credentials, bot tokens, TeraBox credentials, and
`SPIDEY_SESSION_ENCRYPT_KEY` through the deployment secret manager. Changing
the encryption key requires decrypting and re-encrypting stored cookies in a
controlled migration; it does not convert or replace Telethon session files.

## Logs and metrics

Sensitive fields and URL query parameters are redacted by the structured
logging processor. `/stats` shows process-local counters, and
`/metrics` provides Prometheus-compatible text. Queue jobs and these counters
reset when the process restarts.
