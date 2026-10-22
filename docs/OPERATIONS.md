# Operations Runbook

## Startup and readiness

The container is ready only after PostgreSQL connectivity, scheduler startup,
worker startup, and Telegram startup all succeed. The bot then writes
`data/ready`; Docker checks that the recorded process is still alive. A stale
marker is removed at startup and shutdown.

## Event loop

The application uses `winuvloop` at process startup. It selects `uvloop` on
Linux/macOS and `winloop` on Windows, before the bot creates its event loop.
Startup logs include the selected backend. If the accelerator is unavailable,
the application falls back to Python's default asyncio policy and logs a
warning.

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

Telethon file sessions (`user_sessions/*.session`) are plaintext SQLite
databases — the Fernet key does not cover them. To rotate a user session,
delete the file and have the user run `/login` again; to protect the data at
rest, rely on volume permissions or full-disk encryption.

## When the bot goes silent

The process can stay up while answering nothing. Triage with the health data first:

| Observation | Meaning |
|:--|:--|
| `(unhealthy)` with `Health check exceeded timeout`, and `docker stats` shows ~100 % CPU | the **worker loop was spinning** on queued-but-ineligible jobs. Fixed in `queue.wait_for_work()`; if it reappears, `py-spy dump` will show `claim_next` in the hot stack |
| `(unhealthy)` with `Health check exceeded timeout`, CPU normal | the event loop is **blocked** — something synchronous is holding it (hachoir media parsing, ffmpeg, a blocking HTTP client) |
| `(unhealthy)` with a `503` body from `/readyz` | a probe failed; the JSON names the component (`telegram: false` = dropped connection) |
| `(healthy)` but no replies | handler-level exception; check `docker logs` for the traceback |

Useful commands:

```bash
docker inspect --format '{{json .State.Health.Log}}' spideybot | tail -c 600; echo
docker exec spideybot kill -USR1 1 && docker logs --tail 200 spideybot   # thread stacks
docker exec spideybot sh -c 'ls /proc/1/fd | wc -l'     # file-descriptor leak?
docker exec spideybot /opt/venv/bin/python -c \
  "import socket;s=socket.socket();s.settimeout(5);print(s.connect_ex(('91.108.56.163',443)))"
```

`SIGUSR1` is wired to `faulthandler`, so the process prints a full stack dump of every
thread to its log — enough to see what is holding the loop, with no debugger, extra image, or
`SYS_PTRACE` capability. (`py-spy` works too but needs `ghcr.io` reachable and
`--cap-add SYS_PTRACE`; `faulthandler` is stdlib.)

The application logs both failure modes itself:

- `event loop blocked lag_seconds=…` then `event loop recovered blocked_seconds=…` — a synchronous
  call froze the loop (see the watchdog below).
- `bot disconnected, reconnecting attempt=…`, `bot connection restored`, and finally
  `bot could not reconnect — exiting so the container restarts`.

`restart: unless-stopped` does **not** restart an unhealthy container, so the watchdogs (not
Docker) are what recover the process. If the loop is blocked repeatedly, lower
`SPIDEY_MAX_CONCURRENT_DOWNLOADS` (20 → 8) so fewer video pipelines run at once.

## Watchdogs

| Setting | Default | Meaning |
|:--|--:|:--|
| `SPIDEY_LOOP_LAG_THRESHOLD_SECONDS` | 2.0 | log `event loop blocked` once a 0.5 s tick is this late |
| `SPIDEY_WATCHDOG_INTERVAL_SECONDS` | 30 | how often the connection watchdog checks |
| `SPIDEY_WATCHDOG_GRACE_SECONDS` | 60 | tolerate a short Telethon reconnect before acting |
| `SPIDEY_WATCHDOG_MAX_RECONNECTS` | 5 | failed reconnects before the process exits for a container restart |

The same watchdog drops user clients that stay disconnected past the grace period, so they are
rebuilt on demand instead of silently falling back to the shared bot client.

## Docker build cache

The Dockerfile is split so that source edits only redo the cheap layers:

| Stage | Invalidated by |
|:--|:--|
| `deps` | `pyproject.toml` / `uv.lock` only (it copies nothing else) |
| `camoufox` | changes to `deps` — application edits never re-download the browser |
| `builder` | any shipped file (`COPY . .` + a `--no-deps` project install, seconds) |
| `runtime` | the staging copy of the source tree |

`docs/`, `tests/`, `*.md`, `.git/`, `data/`, `downloads/`, `user_sessions/` and `.venv/` are in
`.dockerignore`, so editing them does not start a rebuild at all. To confirm which layers were
reused, build with `--progress=plain` and look for `CACHED`.

## Logs and metrics

Sensitive fields and URL query parameters are redacted by the structured
logging processor. `/stats` shows process-local counters, and
`/metrics` provides Prometheus-compatible text. Queue jobs and these counters
reset when the process restarts.

## Tuning media and send pacing — ✅ implemented

`core/sendgate.py` paces media preparation and sends (see
[SEND_RATE_LIMITING.md](./SEND_RATE_LIMITING.md)). When delivery feels slow or
Telegram starts refusing sends:

| Symptom | Check | Knob |
|:--|:--|:--|
| `spideybot_send_flood_waits_total` climbing | which client | lower `SPIDEY_MEDIA_PER_MINUTE`, or `SPIDEY_MEDIA_CONCURRENCY` for the shared bot client |
| `spideybot_media_wait_seconds_total` large, few floods | token budget too tight | raise `SPIDEY_MEDIA_PER_MINUTE` / `SPIDEY_MEDIA_BURST` |
| `spideybot_media_in_flight` pinned at the slot limit | uploads queued behind slots | raise `SPIDEY_MEDIA_CONCURRENCY` (bot) or the tier's concurrency |
| `spideybot_sessionless_active` at the share while queue depth stays high | sessionless users waiting on the reserved slots | raise `SPIDEY_SESSIONLESS_WORKER_SLOTS` (or `0` to disable the split) |
| `spideybot_sessionless_deferred_total` climbing | sessionless claims deferred — that is the `/login` incentive working | leave it alone unless queue depth grows unbounded |
| `spideybot_active_sessions` pinned at `SPIDEY_SESSION_MAX_LIVE` | the live-client cap, not the idle timeout, is evicting clients (free clients go first) | raise `SPIDEY_SESSION_MAX_LIVE` (memory permitting) or shorten the premium idle |
| large chats stall while others work | per-chat message budget | `SPIDEY_SEND_CHAT_PER_MINUTE` / `SPIDEY_SEND_BURST` |
| `user:<id>` floods, bot is fine | that user's own account | nothing to tune — the cooldown is theirs alone |

A flood on the bot client pauses every sessionless user until the cooldown
expires; users who ran `/login` are unaffected. That asymmetry is intentional —
it is the incentive to log in.

