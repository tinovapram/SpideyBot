# SpideyBot V4 — Architecture

System design, data model, and tier/quota specification for the 1000-user rewrite.

## 1. Process model

- **Single asyncio process** for the bot (Telethon), download workers, and scheduler.
- **Designed for horizontal scale**: the persistent queue uses `FOR UPDATE SKIP LOCKED`,
  so adding worker processes later requires no queue rewrite.
- **Known single-process limits** (documented, not fixed in V4):
  - In-memory per-user rate limiting — for multi-worker you need Redis.
  - In-memory session LRU — a user's session lives on whichever worker claims their job.

```
┌───────────────────────────────────────────────────────────┐
│                      Bot process                          │
│                                                            │
│  Telegram bot (Telethon)                                   │
│        │                                                   │
│  ┌─────▼──────┐   ┌──────────────┐   ┌────────────────┐   │
│  │ handlers   │──▶│  rate limit  │──▶│    quota       │   │
│  └────────────┘   └──────────────┘   └───────┬────────┘   │
│                                              ▼            │
│  ┌──────────────┐   ┌──────────────────────────────┐      │
│  │  scheduler   │   │   persistent priority queue   │      │
│  │ (reset/reap) │   │   (SKIP LOCKED + lease)       │      │
│  └──────┬───────┘   └──────────────┬───────────────┘      │
│         │                          ▼                      │
│  ┌──────▼───────────────────────────────────────┐         │
│  │  download workers → flow → base/site adapters │         │
│  └──────────────────────────────────────────────┘         │
│         │                     │                           │
│  user sessions (LRU)     yt-dlp / gallery-dl / TeraBox    │
└─────────┼─────────────────────┼───────────────────────────┘
          │                     │
   PostgreSQL (users, quota, jobs, history)
```

## 2. Package layout

Kept flat (`core/`, `downloader/`, `handler/`, `utils/`) so `python main.py`,
Docker, and `tests/` import paths stay valid. New modules are added under `core/`.

```
core/
  config.py      # pydantic-settings (SPIDEY_* env)
  tiers.py       # tier + quota policy (single source of truth)
  models.py      # ORM models
  db.py          # async engine/session
  quota.py       # check/reserve/consume/reset (atomic)
  ratelimit.py   # per-user token bucket
  queue.py       # persistent priority queue
  scheduler.py   # background jobs
  metrics.py     # in-process counters
  sessions.py    # on-demand user session lifecycle
  bot.py         # wiring + lifecycle
  logging.py
downloader/
  base.py  registry.py  flow.py  engines.py
  terabox.py  terabox_flow.py  terabox_transfer.py
  site/*.py     # per-site adapters (ported)
handler/
  user.py  admin.py  login.py  outgoing.py
utils/
  files.py  format.py  paths.py  progress.py  telethon.py
migrations/       # Alembic
```

## 3. Data model (PostgreSQL)

```sql
users (
  id            BIGINT PRIMARY KEY,          -- Telegram user id
  username      TEXT,
  tier          TEXT NOT NULL DEFAULT 'free', -- 'free'|'pro'|'premium'
  tier_expiry   TIMESTAMPTZ,                 -- NULL = permanent
  is_admin      BOOLEAN NOT NULL DEFAULT false,
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
)

quota_usage (
  user_id       BIGINT NOT NULL REFERENCES users(id),
  period_start  DATE   NOT NULL,             -- start of day / month
  period_type   TEXT   NOT NULL,             -- 'daily' | 'monthly'
  downloads     INT    NOT NULL DEFAULT 0,
  bytes_down    BIGINT NOT NULL DEFAULT 0,
  bytes_up      BIGINT NOT NULL DEFAULT 0,
  PRIMARY KEY (user_id, period_start, period_type)
)

download_jobs (
  id             BIGSERIAL PRIMARY KEY,
  user_id        BIGINT NOT NULL REFERENCES users(id),
  link           TEXT   NOT NULL,
  site           TEXT,
  tier           TEXT   NOT NULL,
  priority       REAL   NOT NULL,
  status         TEXT   NOT NULL DEFAULT 'queued',
                 -- 'queued' | 'running' | 'done' | 'failed' | 'cancelled'
  attempts       INT    NOT NULL DEFAULT 0,
  total_bytes    BIGINT,
  claim_token    UUID,                        -- worker lease
  lease_expires  TIMESTAMPTZ,
  created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
  started_at     TIMESTAMPTZ,
  finished_at    TIMESTAMPTZ
)
CREATE INDEX idx_jobs_claim ON download_jobs (status, priority, created_at);

download_history (
  id          BIGSERIAL PRIMARY KEY,
  user_id     BIGINT NOT NULL REFERENCES users(id),
  job_id      BIGINT REFERENCES download_jobs(id),
  link        TEXT   NOT NULL,
  site        TEXT,
  filename    TEXT,
  bytes       BIGINT,
  status      TEXT,
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
)
CREATE INDEX idx_history_user ON download_history (user_id, created_at DESC);

referrals (
  id           BIGSERIAL PRIMARY KEY,
  referrer_id  BIGINT NOT NULL REFERENCES users(id),
  invitee_id   BIGINT NOT NULL REFERENCES users(id),
  status       TEXT   NOT NULL DEFAULT 'pending',  -- 'pending' | 'credited' | 'revoked'
  credited_at  TIMESTAMPTZ,
  created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (invitee_id)   -- one referrer per invitee (first start wins)
)
CREATE INDEX idx_referrals_referrer ON referrals (referrer_id);
```

### Why these choices

- **Quota in one table** (`quota_usage`) with a `(user_id, period_start, period_type)`
  primary key → atomic upserts, trivial rollover, no per-day tables.
- **`download_jobs.claim_token` + `lease_expires`** → crash-safe queue: a worker leases a
  job; if it dies, the scheduler re-queues expired leases.
- **`download_history` separate from jobs** → retention can delete history without
  touching queue bookkeeping.

## 4. Tier & quota specification

Tiers are **data**, loaded from `core/tiers.py` (overridable via env). Admin bypass is a
boolean, not a tier.

| Dimension | free | pro | premium |
|:--|:--|:--|:--|
| per-file size limit | 100 MB | 2 GB | 10 GB |
| per-link total size | 500 MB | 10 GB | unlimited |
| daily downloads | 10 | 50 | 200 |
| daily bandwidth | 1 GB | 25 GB | 100 GB |
| concurrent downloads | 1 | 3 | 8 |
| queue priority | 2.0 | 1.0 | 0.5 (admin 0.2) |

All tier parameters are configurable via `SPIDEY_TIER_*` env vars.

```python
@dataclass(frozen=True)
class QuotaPolicy:
    link_total_limit_bytes: int | None      # None = unlimited
    daily_downloads: int | None             # None = unlimited
    daily_bytes: int | None                 # None = unlimited
    concurrent: int
    priority: float

TIERS: dict[str, QuotaPolicy] = { "free": ..., "pro": ..., "premium": ... }
```

All users can access all sites — no site gating.

## 5. Quota flow (per download request)

```
request → rate-limit check (429 if exceeded)
        → quota check (daily count · daily bytes · concurrent slots)
        → enqueue job (priority = tier.priority)
        → worker claims job (SKIP LOCKED)
        → download → upload → record bytes in quota_usage
        → release slot
```

## 6. Persistent queue

- Heap ordering (`priority`, `created_at`) in SQL, not in memory.
- Workers claim with:
  ```sql
  SELECT ... FROM download_jobs
  WHERE status = 'queued' AND (lease_expires IS NULL OR lease_expires < now())
  ORDER BY priority, created_at
  FOR UPDATE SKIP LOCKED
  LIMIT 1
  ```
- Each claim sets `status='running'`, a fresh `claim_token`, and `lease_expires = now() + lease_timeout`.
- On completion/failure the worker finalizes the job; a sweeper re-queues expired leases.

## 7. User session lifecycle (1000-user fix)

- `_live_clients` is an **LRU with a hard cap** (e.g. 50 live clients) and an **idle
  timeout** (e.g. 10 min).
- Sessions start **on demand** when a job needs the user's account; not on `/start`.
- `get_dialogs()` is called only when actually needed (deferred).
- A scheduler task sweeps idle clients every minute and disconnects them.
- When the cap is hit, the least-recently-used client is evicted (disconnected), never
  errored.

This replaces V3's "every logged-in user stays connected forever".

## 8. Rate limiting

- Per-user token bucket in memory: `MAX_COMMANDS_PER_MINUTE` (default 12) burstable.
- Applies to commands that enqueue work (`/dl`, `/dt`, `/login`).
- 429-style reply when exceeded. Documented as single-process (Redis is the multi-worker path).

## 9. Referral system

- Link format: `https://t.me/<bot_username>?start=<code>` where `<code>` is the
  referrer's Telegram user ID (base62 obfuscation is a later option).
- Detection: Telegram delivers the payload as the message text `/start <code>`; the
  `/start` handler parses the argument.

**Flow**

1. User A runs `/referral` → bot returns their link + credited/pending counts.
2. User B opens A's link → Telegram auto-sends `/start <A_id>`.
3. `/start` handler, when a payload is present and valid:
   - ignores if `code == B_id` (self-referral),
   - ignores if B already has a referrer (first start wins),
   - otherwise inserts `referrals(referrer=A, invitee=B, status='pending')`.
4. When B completes their **first successful download**, the referral is marked
   `credited` and A receives the configured reward.

**Reward (configurable)**

- Default: referrer gets a one-time quota bonus, e.g. `+10 daily downloads` for
  `30 days`, per credited referral.
- Reward lives in `core/tiers.py` as `REFERRAL_REWARD`, admin-overridable via config.

**Anti-abuse**

- `UNIQUE (invitee_id)` — an invitee can only ever credit one referrer.
- Self-referral blocked.
- Credit requires invitee's **first successful download**, not just joining.
- Invitee must be genuinely new (no prior `users` row).

## 10. Config (pydantic-settings)

```
SPIDEY_TG_API_ID, SPIDEY_TG_API_HASH, SPIDEY_TG_BOT_TOKEN
SPIDEY_DATABASE_URL=postgresql+asyncpg://...
SPIDEY_SESSION_ENCRYPT_KEY
SPIDEY_TERABOX_COOKIES (single or |-delimited), SPIDEY_TERABOX_*
SPIDEY_ADMIN_IDS
SPIDEY_MAX_CONCURRENT_DOWNLOADS
SPIDEY_RATE_LIMIT_PER_MINUTE
SPIDEY_SESSION_MAX_LIVE / _IDLE_SECONDS
SPIDEY_RETENTION_DAYS
SPIDEY_TIER_OVERRIDES (JSON)   # optional per-tier policy override
```

All V3 `os.getenv` soup is replaced by typed fields with validation.
