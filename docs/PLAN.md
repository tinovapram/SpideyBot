# SpideyBot V4 — Plan

> Full rewrite for 1000-user scale + tier-based quota.
> Branch: `V4` · Storage: PostgreSQL · Tiers: free / pro / premium (+ admin bypass)

## Objectives

1. Handle **1000 active users** without degrading under burst load.
2. Replace the boolean `is_premium` model with a **data-driven tier + quota** system.
3. Full rewrite of core **and** all site downloaders.
4. Add persistent queue, rate limiting, usage history, stats, retry, dedupe, retention.

## Scope decisions (locked)

| Decision | Choice |
|:--|:--|
| Tiers | `free`, `pro`, `premium` (+ admin bypass) |
| Quota dimensions | per-file size · per-link total size · daily count · daily bandwidth · monthly bandwidth · concurrent · site gating · priority · history retention |
| Storage | PostgreSQL (async SQLAlchemy 2.0 + asyncpg) |
| Rewrite | Everything — core + per-site adapters |
| Extras | persistent queue · per-user rate limiting · `/stats` · `/quota` · `/history` · retry/backoff · dedupe · retention |
| Referral | deep link `?start=<code>`, reward on invitee's first successful download |
| Out of scope | batch/multi-link · playlist support |

## Phases

### Phase 0 — Foundations
- [ ] `config.py` → pydantic-settings, typed, env-prefixed (`SPIDEY_*`)
- [ ] `tiers.py` → tier table + quota policies (single source of truth)
- [ ] `models.py` → ORM schema (users, quota_usage, download_jobs, history, referrals)
- [ ] `db.py` → async engine + session factory
- [ ] Alembic migrations (baseline + forward-only)
- [ ] `utils/paths.py` → dirs + data layout
- [ ] `docker-compose.yml` → add `postgres` service
- **Done when**: config loads, schema migrates against a real Postgres.

### Phase 1 — Data layer
- [ ] `core/quota.py` → check / reserve / consume / reset (atomic SQL)
- [ ] `core/ratelimit.py` → per-user token bucket
- [ ] `core/referral.py` → referral graph, reward credit, anti-abuse
- [ ] `core/models.py` helpers (upsert user, tier lookup)
- **Done when**: unit tests prove quota decrements/resets and referral credit are correct.

### Phase 2 — Queue & scheduler
- [ ] `core/queue.py` → persistent priority queue (`FOR UPDATE SKIP LOCKED` + lease)
- [ ] `core/scheduler.py` → background jobs (quota reset, idle sweep, retention)
- [ ] `core/metrics.py` → in-process counters for `/stats`
- **Done when**: jobs survive restart; two workers can share a queue safely.

### Phase 3 — Downloader core
- [ ] `downloader/base.py` → `BaseDownloader` (async, quota-aware)
- [ ] `downloader/registry.py` → host → adapter registry + tier gating
- [ ] `downloader/flow.py` → orchestration (resolve → download → upload → history)
- [ ] `downloader/engines.py` → yt-dlp / gallery-dl wrappers
- [ ] `downloader/terabox*.py` → port TeraBox (transfer backends, account pool)
- [ ] Port all per-site adapters (`downloader/site/*.py`)
- **Done when**: every V3 site adapter has a V4 equivalent passing a smoke test.

### Phase 4 — Sessions
- [ ] `core/sessions.py` → on-demand lifecycle (idle timeout + LRU + live cap)
- [ ] `handler/login.py` → login / logout / ping flow
- **Done when**: max concurrent live user clients is bounded and idle clients are reaped.

### Phase 5 — Handlers
- [ ] `handler/user.py` → `/start /stop /help /dl /dt /cancel /quota /history /sites /referral`
- [ ] `/start` deep-link parsing: `?start=<code>` → record referral
- [ ] `handler/admin.py` → `/addtier /settier /setquota /resetquota /stats /ban`
- [ ] `handler/outgoing.py` → progress / upload status
- **Done when**: full command surface is wired and quota-aware.

### Phase 6 — Extras
- [ ] Retry with exponential backoff
- [ ] Duplicate-link detection (hash of normalized link within window)
- [ ] Retention policy (auto-clean `downloads/` + history)
- [ ] `/stats` dashboard
- **Done when**: all extra features have tests.

### Phase 7 — Tests, migration, cutover
- [ ] Port + extend the 96 existing tests
- [ ] Add quota / queue / session / rate-limit test suites
- [ ] V3 → V4 data migration script (users, tier, sessions unchanged)
- [ ] Docker build + `docker compose up` verification
- [ ] Docs: README, `.env.example`, tier guide
- **Done when**: CI-green and a side-by-side cutover run succeeds.

## Deliverables

- `docs/PLAN.md` (this file)
- `docs/ARCHITECTURE.md` — system design + data model + tier spec
- `docs/STRATEGY.md` — execution order, risks, migration, rollback
- New V4 source tree (full rewrite)
- Alembic migrations
- Referral system (deep link + reward + anti-abuse)
- Updated `docker-compose.yml` (Postgres + bot)
- Test suite ≥ V3 coverage

## Milestones

| Milestone | Exit criterion |
|:--|:--|
| M1: Foundations | config + schema migrate cleanly |
| M2: Data layer | quota + rate limit unit-tested |
| M3: Queue | persistent queue survives restart |
| M4: Downloader | all adapters smoke-test green |
| M5: Sessions | live-client cap + idle reaping |
| M6: Handlers | full command surface wired |
| M7: Cutover | V3 → V4 migration + compose run |
