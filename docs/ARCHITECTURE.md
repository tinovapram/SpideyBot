# SpideyBot — Architecture

System design, data model, and tier/quota specification for the current V3 code base.

## 1. Process model

- **Single asyncio process** for the bot (Telethon), download workers, and scheduler.
- **Process-local queue**: download jobs are intentionally discarded when the process stops.
- **Known single-process limits** (documented, not addressed by this design):
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
│  │  scheduler   │   │ process-local priority queue │      │
│  │ (quota sweep)│   │   (fresh after restart)       │      │
│  └──────┬───────┘   └──────────────┬───────────────┘      │
│         │                          ▼                      │
│  ┌──────▼───────────────────────────────────────┐         │
│  │  download workers → flow → base/site adapters │         │
│  └──────────────────────────────────────────────┘         │
│         │                     │                           │
│  user sessions (LRU)     yt-dlp / gallery-dl / TeraBox    │
└─────────┼─────────────────────┼───────────────────────────┘
          │                     │
   PostgreSQL (users, quota, referrals)
```

## 2. Package layout

Kept flat (`core/`, `downloader/`, `handler/`, `utils/`) so `python main.py`,
Docker, and `tests/` import paths stay valid. New modules are added under `core/`.

```
core/
  config.py        # pydantic-settings (SPIDEY_* env), unknown-key warnings
  tiers.py         # tier + quota policy (single source of truth)
  models.py        # ORM models
  db.py            # async engine/session
  quota.py         # check/reserve/consume/reset (atomic)
  ratelimit.py     # per-user token bucket
  sendgate.py      # per-client upload/send pacing, flood cooldown, retries
  queue.py         # process-local priority queue + worker wake-up
  worker.py        # DownloadManager: claims jobs, drives the flows
  scheduler.py     # background maintenance
  metrics.py       # counters + Prometheus text export
  observability.py # /livez, /readyz, /metrics endpoints + probes
  sessions.py      # on-demand user session lifecycle
  cookies.py       # encrypted per-user TeraBox cookies
  referral.py      # invite links, credit on first download, bonus math
  event_loop.py    # platform-specific loop setup (uvloop / winuvloop)
  bot.py           # wiring + lifecycle
  logging.py
downloader/
  base.py  registry.py  flow.py  proxy.py
  gallerydl.py  telegram.py
  terabox.py  terabox_flow.py  terabox_transfer.py
  site/*.py        # per-site adapters
handler/
  handler.py       # /dl, /dt, /start, /quota, /referral, magic word, timer media
  admin.py  login.py  setting.py  bypass.py
utils/
  files.py  format.py  paths.py  progress.py  telethon.py  urls.py
migrations/       # Alembic
```

## 3. Data model (PostgreSQL)

```sql
users (
  id                  BIGINT PRIMARY KEY,      -- Telegram user id
  username            TEXT,
  tier                TEXT NOT NULL DEFAULT 'free', -- 'free'|'pro'|'premium'
  tier_expiry         TIMESTAMPTZ,             -- NULL = permanent
  is_admin            BOOLEAN NOT NULL DEFAULT false,
  timer_media_enabled BOOLEAN NOT NULL DEFAULT true,  -- auto-save private media
  magic_word          TEXT NOT NULL DEFAULT 'WOW',    -- reply-to-download trigger
  created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
)

user_cookies (
  user_id          BIGINT PRIMARY KEY REFERENCES users(id),
  cookie_encrypted TEXT NOT NULL,              -- Fernet(ndus=...)
  created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at       TIMESTAMPTZ NOT NULL DEFAULT now()
)
-- Free-tier cookies join the shared TeraBox pool; pro/premium cookies stay private.

quota_usage (
  user_id       BIGINT NOT NULL REFERENCES users(id),
  period_start  DATE   NOT NULL,             -- start of day / month
  period_type   TEXT   NOT NULL,             -- 'daily' (the 'monthly' key is reserved, unused)
  downloads     INT    NOT NULL DEFAULT 0,
  bytes_down    BIGINT NOT NULL DEFAULT 0,
  bytes_up      BIGINT NOT NULL DEFAULT 0,
  PRIMARY KEY (user_id, period_start, period_type)
)

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

## 4. Tier & quota specification

Tiers are **data**, loaded from `core/tiers.py` (overridable via env). Admin bypass is a
boolean, not a tier.

| Dimension | free | pro | premium |
|:--|:--|:--|:--|
| per-link total size | 500 MB | 10 GB | unlimited |
| daily downloads | 10 | 50 | 200 |
| daily bandwidth | 1 GB | 25 GB | 100 GB |
| concurrent downloads | 1 | 2 | 3 |
| queue priority | 2.0 | 1.0 | 0.5 (admin 0.2) |
| slots full (`*_CONCURRENT_MODE`) | reject | reject | reject |

`concurrent_mode` can be set to `queue` per tier, in which case a request waits for a slot
instead of being rejected at admission. Admins are the exception to the admission check: they
use Premium's three slots but are never rejected — their extra requests queue at claim time.

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

All sites are available to every tier. The one exception is a **credential gate, not a tier
gate**: TeraBox downloads require the user's own valid `ndus` cookie (`/setting` → TeraBox
cookie), because TeraBox quotas are per account. Admins are exempt and may use the bot's
configured pool (`SPIDEY_TERABOX_COOKIES`). A cookie that is missing, undecryptable or malformed
blocks the request at `/dl` and again at claim time.

## 5. Quota flow (per download request)

```
request → rate-limit check (429 if exceeded)
        → quota check (daily count · daily bytes · concurrent slots)
        → enqueue job (priority = tier.priority)
        → process-local worker claims job
        → download → upload → record bytes in quota_usage
        → release slot
```

`claim_next()` also enforces the **sessionless worker share**: at most
`SPIDEY_SESSIONLESS_WORKER_SLOTS` (default 5) of the workers run jobs for users without a live
Telegram session, so the remaining slots stay reserved for logged-in users. It is a hard cap
(no borrowing) and `0` disables it; admins are exempt. Sessionless users see the split in the
"Queued — position #N" reply and in `/quota`.

A worker that cannot claim **must block**, and `wait_for_work()` enforces that by evaluating the
same eligibility rules as `claim_next()` (shared `_running_state` / `_sessionless_cap_hit` /
`_split_candidates` helpers). Returning early because *some* job merely exists in the queue burned
~110 % of one core with 20 workers once either the per-user slots or the sessionless share were
full, which starved the event loop until Telegram dropped the connection and `/readyz` timed out.
`_worker_loop` keeps a last-resort guard: if a claim is lost to a race, it sleeps briefly instead
of iterating without awaiting.

## 6. User session lifecycle (1000-user fix)

- `_active_clients` is an **LRU with a configured hard cap** and an **idle timeout** that is
  **per tier**: 30 min (free), 12 h (pro), 24 h (premium; admins resolve to premium).
- Sessions start **on demand** when a job needs the user's account; not on `/start`.
- `get_dialogs()` is called only when actually needed (deferred).
- A scheduler task sweeps idle clients every minute and disconnects them, resolving tiers for
  the live clients in one query (only when a client is older than the shortest timeout).
- Activity on the user's **own** client (magic word, timer-media, user-side commands) refreshes
  the idle timer, and so does finishing a job — a long download cannot make the client look idle.
- When the cap is hit, the **least-recently-used free client** is evicted first (then pro, then
  premium, oldest first within each tier); the user being admitted is never evicted, and leased
  clients are skipped.
- A running job **leases** its client, so neither the sweep nor the LRU cap can disconnect it
  mid-upload.
- `/stop` disconnects the live client but keeps `user_sessions/<id>.session`, so `/start`
  reconnects without a new login; `/logout` deletes it.

This replaces V3's "every logged-in user stays connected forever".

## 7. Operations endpoints

- `/livez` reports that the process is serving requests.
- `/readyz` reports readiness live on every request: it checks the ready marker, pings the
  database (`SELECT 1`) and evaluates registered probes (the Telegram connection). A dropped
  dependency turns it into a 503.
- `/metrics` exposes process-local Prometheus-compatible counters and gauges.

## 8. Rate limiting

- Per-user token bucket in memory: `SPIDEY_RATE_LIMIT_PER_MINUTE` (default 12), burstable.
- Applies to the paths that start work: `/dl`, the user-session `/dt` and the magic-word reply.
- A retry-hint reply is sent when the bucket is empty. Documented as single-process (Redis is
  the multi-worker path).

**Upload and send pacing** — ✅ implemented in [`core/sendgate.py`](../core/sendgate.py) (phase 1;
adaptive rates in §8 of that note are ⏳). See
[SEND_RATE_LIMITING.md](./SEND_RATE_LIMITING.md).

- The limited unit is **one prepared media** (one file, whether it is later sent in an album or
  on its own); sends are paced per chat only.
- One `SendGate` per Telethon client — a flood on the bot client cannot stall a user session.
- Media slots (`SPIDEY_MEDIA_CONCURRENCY` on the bot client, the tier's `concurrent` value on a
  user client) bound transfer concurrency; `SPIDEY_MEDIA_PER_MINUTE` bounds how many media start
  per minute; a per-chat bucket bounds message RPCs.
- Shared flood cooldown (one wake-up for all waiters, jittered); retries re-send the
  already-uploaded media; an expired file reference fails fast (`FileExpiredError`) and
  transient errors back off exponentially.
- `core/sessions.py` leases a user client for the job's duration, so the idle sweep and the LRU
  cap cannot disconnect it mid-upload.

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

- Each credited referral adds `SPIDEY_REFERRAL_DAILY_BONUS` (default 10) extra downloads per
  day to the referrer, for `SPIDEY_REFERRAL_BONUS_DAYS` (default 30) days after the credit.
- The bonus is **read-computed** by `core/referral.py::bonus_downloads()` — there is no daily
  re-grant job; `core/quota.py` simply adds the headroom when admitting a download.

**Anti-abuse**

- `UNIQUE (invitee_id)` — an invitee can only ever credit one referrer.
- Self-referral blocked.
- Credit requires the invitee's **first successful download**, not just joining.
- Deep links naming a user who never started the bot are ignored (no foreign-key error).

## 10. Config (pydantic-settings)

```
SPIDEY_TG_API_ID, SPIDEY_TG_API_HASH, SPIDEY_TG_BOT_TOKEN
SPIDEY_DATABASE_URL=postgresql+asyncpg://...   # required, no default
SPIDEY_SESSION_ENCRYPT_KEY
SPIDEY_TERABOX_COOKIES (single or |-delimited), SPIDEY_TERABOX_{JSTOKEN,BDSTOKEN,SIGN,TIMESTAMP,LOGID}
SPIDEY_ADMIN_IDS
SPIDEY_MAX_CONCURRENT_DOWNLOADS, SPIDEY_JOB_MAX_ATTEMPTS, SPIDEY_PROXY_POOL
SPIDEY_RATE_LIMIT_PER_MINUTE
SPIDEY_MEDIA_CONCURRENCY, SPIDEY_MEDIA_PER_MINUTE, SPIDEY_MEDIA_BURST
SPIDEY_SEND_CHAT_PER_MINUTE, SPIDEY_SEND_BURST
SPIDEY_FLOOD_JITTER_SECONDS, SPIDEY_SEND_MAX_ATTEMPTS
SPIDEY_SESSIONLESS_WORKER_SLOTS
SPIDEY_SESSION_MAX_LIVE / _IDLE_SECONDS
SPIDEY_METRICS_HOST / _PORT
SPIDEY_LOOP_LAG_THRESHOLD_SECONDS, SPIDEY_WATCHDOG_{INTERVAL,GRACE}_SECONDS
SPIDEY_WATCHDOG_MAX_RECONNECTS
SPIDEY_TIER_{FREE,PRO,PREMIUM}_*   # limits, priority, concurrent mode
SPIDEY_CAMOUFOX_MAX_CONCURRENCY / _HEADLESS
SPIDEY_DEFAULT_TIMER_MEDIA / _MAGIC_WORD
SPIDEY_REFERRAL_DAILY_BONUS / _BONUS_DAYS
```

Every `os.getenv` read was replaced by a typed field with validation; unknown `SPIDEY_*` keys
are logged at startup instead of being silently ignored. `.env.example` is the authoritative
list.
