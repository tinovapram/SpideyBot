# SpideyBot V4 — Strategy

How we execute the rewrite, manage risk, migrate from V3, and verify success.

## 1. Execution approach

### 1.1 Build bottom-up, in dependency order

The phases in `PLAN.md` are ordered so each one is independently testable:

```
Foundations → Data layer → Queue/scheduler → Downloader → Sessions → Handlers → Extras → Cutover
```

Rationale: the hardest-to-change pieces (config, schema, quota semantics) go first, because
everything above them depends on their contracts. Per-site downloaders go last in the core
build because they depend on `BaseDownloader`, `flow`, and `registry` interfaces being stable.

### 1.2 Replace, don't refactor

- V4 is a **new tree**, written fresh; we do not surgically edit V3 files.
- V3 stays on branch `V3` untouched until cutover, giving an instant rollback path.

### 1.3 Port downloaders mechanically first, improve second

- Step A: port each V3 `downloader/site/*.py` to the new `BaseDownloader` interface with
  behavior-preserving changes only.
- Step B: after the whole suite is green, do targeted improvements (quota hooks, retry).

This keeps "rewrite everything" from turning into "rewrite everything and also break
undocumented edge cases" — the existing 96 tests are the regression net.

## 2. Risk register

| # | Risk | Likelihood | Impact | Mitigation |
|:--|:--|:--|:--|:--|
| R1 | Postgres adds ops burden / not available in dev | High | Med | `docker compose` ships a `postgres` service; docs cover local setup |
| R2 | Telethon user-session flood at scale | High | High | On-demand start + idle timeout + LRU cap + live-cap (Phase 4) |
| R3 | Quota oversell under concurrency | Med | High | Atomic `INSERT ... ON CONFLICT` with `WHERE` guard; tests with concurrent writers |
| R4 | Queue job loss on crash | Med | High | `SKIP LOCKED` + lease timeout + sweeper re-queue |
| R5 | Per-site downloader regressions during rewrite | High | Med | Port-first, improve-second; keep V3 tests as regression net |
| R6 | In-memory rate limit / session LRU drift across workers | Low | Med | Documented; Redis is the documented multi-worker path (out of V4 scope) |
| R7 | Data migration from V3 loses premium state | Med | High | Migration script maps `is_premium → tier='premium'` + expiry; dry-run + verify |
| R8 | Referral farming / self-referral abuse | Med | Med | First-successful-download credit, `UNIQUE(invitee_id)`, self-referral block |

## 3. Migration from V3

### 3.1 What carries over

- `user_sessions/*.session` — unchanged file format (Telethon SQLite sessions).
- `config/runtime`, gallery-dl cache — unchanged.
- `downloads/` — ephemeral staging, not migrated.

### 3.2 Users table

| V3 field | V4 mapping |
|:--|:--|
| `user_id` | `users.id` |
| `username` | `users.username` |
| `is_premium = true` | `users.tier = 'premium'` |
| `premium_expiry` | `users.tier_expiry` |
| — | `users.tier = 'free'` (default) |
| in `ADMIN_IDS` env | `users.is_admin = true` |

### 3.3 Cutover procedure

1. Bring up V4 + Postgres alongside V3 (different bot token or staged rollout).
2. Run the migration script against the V3 SQLite DB → populate Postgres.
3. Smoke-test a sample of users/tiers/quota in V4.
4. Swap `docker compose` to V4; keep V3 image tag available for rollback.
5. Rollback = `git checkout V3` + restore the V3 SQLite file (V3 never reads Postgres).
6. Referral is **new in V4** — no V3 data to migrate; the `referrals` table starts empty.

## 4. Testing strategy

| Layer | Tool | Coverage target |
|:--|:--|:--|
| Config/tiers | pytest | every tier + override path |
| Quota | pytest + concurrent writers | oversell must not happen |
| Rate limit | pytest (fake clock) | bucket refill + 429 path |
| Queue | pytest + two worker loops | no double-claim; lease re-queue |
| Sessions | pytest | LRU eviction + idle reaping |
| Downloaders | smoke tests per adapter | URL → resolved file path |
| Handlers | Telethon event fixtures | command surface + quota denials |
| Migration | pytest | V3 SQLite → V4 Postgres mapping |
| Referral | pytest | deep-link parse, first-win, self-block, first-download credit |

- Keep `pytest` + `asyncio_mode=auto` (already configured).
- Postgres tests run against a disposable test DB (env `SPIDEY_DATABASE_URL`).

## 5. Verification of the 1000-user goal

- **Load test**: scripted burst of N simulated users hitting `/dl` → assert queue depth
  stays bounded, no job loss, p95 latency under a target.
- **Session cap test**: drive 200 logins → assert live client count never exceeds the cap
  and evicted sessions reconnect on demand.
- **Quota test**: a free user's 11th daily download is rejected; a premium user's 201st is
  rejected; bandwidth caps hold.

## 6. Definition of done (V4)

- [ ] All `PLAN.md` phases complete with tests green.
- [ ] `docker compose up` runs bot + Postgres + migrations idempotently.
- [ ] V3 → V4 migration verified on real data.
- [ ] 1000-user load test passes the session-cap and quota assertions.
- [ ] README + `.env.example` + tier guide updated.
- [ ] V4 merged; V3 retained as rollback branch.
