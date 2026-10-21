# SpideyBot Project Improvement Suggestions

This document records project-wide improvement recommendations identified during the audit of SpideyBot.

- **Audit scope:** branch `V3.0` at `c82b5a1`, 103 tracked files, ~7.3k lines of Python.
- **Method:** static reading of `core/`, `downloader/`, `handler/`, `utils/`, Alembic migrations, Docker/CI config and docs; plus execution of `ruff`, `pytest`, `git ls-files`, and inspection of the GitHub Actions history.
- **Status:** findings are prioritised P0 (broken/blocking) → P3 (hygiene). Each item lists *evidence* and a *suggested fix*.

## Health snapshot

| Check | Result |
|:--|:--|
| CI on `V3.0` | **Red — 6/6 runs failed**, always in the `Lint` step (`Found 288 errors`); `pyright` and `pytest` never execute |
| `pytest` (local) | 11 tests collected, **8 passed, 3 errors** (`ModuleNotFoundError: No module named 'click'`) |
| `ruff check .` | 288 findings; 125 auto-fixable (`BLE001` 113, `UP045` 55, `S110` 26, `UP017` 16, `I001` 14, …) |
| Test suite size | 11 tests vs. "148 tests" claimed in the README |
| Packaging | No `[tool.ruff]` / `[tool.pyright]` config, no lockfile, no `LICENSE`, no `py.typed` |

Reproduce with:

```bash
ruff check .
pytest -q
```

**Post-fix status (2026-10-22, P0–P3 resolved):** `ruff check .` clean, `pytest -q` → 47 passed,
`pyright` → 77 pre-existing type errors (advisory, no undefined names), CI installs from
`uv.lock` into `.venv` and gates on lint.

---

## P0 — Bugs that block or break features

> **All P0 items below are fixed.** Each section keeps the original evidence and adds a
> `Status` note describing what actually changed and how it was verified.

### P0-1. `click.Path` is used as `pathlib.Path` (and `click` is not a dependency)

> **Status: ✅ Done** — the `click` import is gone and `handler.handler` imports with no
> `click` installed (verified with an interpreter where `click` is absent, which also turned
> the three failing tests green). Timer media is now written under
> `downloads/tg_<user id>/timermedia_<sender>_<message>.ext` and the staging file is removed
> after it is forwarded to Saved Messages.

**Evidence:** [handler/handler.py:25](../handler/handler.py) imports `from click import Path`. `core/bot.py` imports `handler.handler` during startup, so a clean install without `click` fails to boot (`ModuleNotFoundError`); this is also the cause of the 3 `pytest` errors today.

**Impact:** [handler/handler.py:685](../handler/handler.py) does `str(Path(f"TimerMedia/from..."))`. With `click.Path`, the string is stored as its `exists` argument, so `str()` yields `<click.types.Path object at 0x…>`. Auto-saved timer media is written to a garbage filename in the process CWD instead of `DOWNLOADS_DIR`.

**Fix:** replace the import with `from pathlib import Path`; build the destination as `DOWNLOADS_DIR / f"tg_{...}"` and ensure the directory exists.

### P0-2. Per-user rate limiting is dead code

> **Status: ✅ Done** — `Handler._rate_limited()` now guards every download entry point
> (`/dl`, the Telegram `/dt` path and the magic-word reply), replies with a retry hint and
> increments `metrics.rate_limited`. The token bucket itself is covered by
> `tests/test_ratelimit.py` (capacity, per-user isolation, refill, reset).

**Evidence:** [core/ratelimit.py](../core/ratelimit.py) is only referenced by [`core/scheduler.py`](../core/scheduler.py) for `clear_stale()`. `RateLimiter.allow()` has no caller, and `Metrics.rate_limited` is never incremented.

**Impact:** `SPIDEY_RATE_LIMIT_PER_MINUTE` has no effect. The README advertises "Rate limiting — Per-user rate limits to prevent abuse", so abuse protection is missing entirely.

**Fix:** call `get_limiter().allow(user_id)` in `Handler._link_command` and `magic_user_handler` (and the user-session variants), reply with a 429-style message, and increment `metrics.rate_limited`.

### P0-3. Referral rewards are never awarded

> **Status: ✅ Done** — `/start <id>` now parses the deep-link payload and records a pending
> referral (payloads naming a user who never started the bot are ignored instead of tripping
> the foreign key), and the worker calls `try_credit_on_download()` on the first successful
> download, so `bonus_downloads()` starts returning the configured bonus. Covered by two
> database tests in `tests/test_quota_sql.py`.

**Evidence:** `record_referral` ([core/referral.py:53](../core/referral.py)) and `try_credit_on_download` ([core/referral.py:87](../core/referral.py)) have **no callers**. `parse_start_payload` is likewise unused, so `/start <referrer_id>` ignores its payload, and no successful download credits a referrer.

**Impact:** `bonus_downloads()` always returns 0 — the `/referral` command shows stats, but the reward loop does not exist.

**Fix:** in `start_handler`, parse the `/start` payload and call `record_referral`; in the worker success path, call `try_credit_on_download` before/with `consume(...)`.

### P0-4. Unbounded memory growth in the process-local queue

> **Status: ✅ Done** — `_update_gauges()` takes an optional `discard` job id and
> `complete()`, `fail()` (terminal) and `cancel()` drop the job while holding the queue lock,
> so the dict no longer grows with the process lifetime. Regression test:
> `test_finished_jobs_are_discarded_from_the_process_queue`.

**Evidence:** jobs are inserted at [core/queue.py:67](../core/queue.py) and never removed — `complete()`, `fail()`, and `cancel()` only mutate `job.status`. `_jobs` is cleared only by `clear()` (tests/shutdown).

**Impact:** every download of the process lifetime stays referenced, so a long-running bot leaks memory linearly with download count.

**Fix:** drop the job from `_jobs` on terminal state (after metrics/queue-depth update), or keep a bounded `OrderedDict` / deque of finished jobs with a max length.

### P0-5. Blocking subprocess call inside async upload path

> **Status: ✅ Done** — `prepare_media()` now runs `make_video_thumb()` through
> `asyncio.to_thread`, so ffmpeg no longer blocks the event loop for other downloads.

**Evidence:** [utils/files.py:149](../utils/files.py) runs blocking `subprocess.run([... "ffmpeg" ...], timeout=30)`; it is called synchronously from async `prepare_media` ([utils/files.py:212](../utils/files.py)).

**Impact:** one large video thumbnail generation blocks the event loop for up to 30 s, stalling every concurrent download, upload, and status edit.

**Fix:** `await asyncio.create_subprocess_exec(...)`, or `await asyncio.to_thread(make_video_thumb, path)`.

### P0-6. The CI pipeline cannot pass

> **Status: ✅ Done** — `pyproject.toml` now pins `ruff==0.17.0` / `pyright==1.1.414` and
> declares `[tool.ruff]` (`select = ["E4","E7","E9","F","I","B","UP"]`) plus `[tool.pyright]`
> (`basic`, `venvPath`/`venv`). All 138 findings were fixed (118 auto, 20 by hand), so lint is
> now a blocking gate. CI installs into `.venv` so local and CI resolve identical packages.
>
> **Open follow-up:** `pyright` is advisory (`continue-on-error`) at a baseline of **78**
> pre-existing type errors — none are undefined names. Drop `continue-on-error` once the
> count reaches zero; keep the number in this document as the tracking baseline.

**Evidence:** `.github/workflows/ci.yml` runs `ruff check .` with no project configuration; none of the 6 workflow runs has ever succeeded. Ruff's default rule set changed between releases, turning a green branch red without any code change.

**Impact:** no merge gate — tests and `pyright` have not run in CI at all.

**Fix:**
1. Add `[tool.ruff]` to `pyproject.toml` (`target-version = "py313"`, explicit `select`, justified `ignore` such as `BLE001` for best-effort Telegram calls) and pin `ruff`/`pyright` in the `test` extra.
2. Fix the 288 findings (`ruff check --fix` handles 125).
3. Add `[tool.pyright]` (or a `pyrightconfig.json`) so type checking is reproducible.

---

## P1 — Reliability and concurrency

> **All P1 items below are fixed.** As with P0, each section keeps the original evidence and
> adds a `Status` note.

### P1-1. `/dt` downloads with the bot client from a user-session handler

> **Status: ✅ Done** — `_download_tg` now uses `event.client` for the download, the media
> upload and the final send, so a user-session `/dt` runs entirely on the user's own account.

`_download_tg` ([handler/handler.py:221](../handler/handler.py)) is reached from `dt_user_handler`, but it downloads and uploads with `self.client` (the shared bot client). A user session exists precisely to read that user's private messages, so this path should use `event.client`.

### P1-2. TeraBox shared-cookie pool is rebuilt per job and mutated concurrently

> **Status: ✅ Done** — `terabox_pool_with_shared()` replaces `_append_shared_cookies()`:
> resolved downloaders are cached per cookie, the combined pool is reused for 60 s
> (`_SHARED_POOL_TTL_SECONDS`), refreshes happen under a lock, the base pool is never mutated,
> and downloaders whose cookie disappeared are closed. The unused `build_terabox_pool()` helper
> was removed.

`_append_shared_cookies` ([core/worker.py:197](../core/worker.py)) re-reads every user cookie for each TeraBox job, constructs new `TeraBoxDownloader` instances, and reassigns `pool.accounts` — while up to `SPIDEY_MAX_CONCURRENT_DOWNLOADS` (20) workers do the same. This races, duplicates accounts, and churns aiohttp sessions that are never closed.

**Fix:** cache the shared pool with a short TTL (e.g. 60 s), rebuild under a lock, and close replaced downloaders.

### P1-3. N+1 queries in `shared_cookies`

> **Status: ✅ Done** — one `UserCookie ⨝ users` query returns cookie, tier and expiry together;
> the admin filter runs in SQL and the effective-tier check stays in Python. Covered by
> `test_shared_cookies_exclude_admins_and_paid_tiers`.

[core/cookies.py:111](../core/cookies.py) selects all `UserCookie` rows, then performs one `session.get(User, …)` per row on every call. Replace with a single join filtering `users.is_admin = false` and effective tier `free`.

### P1-4. Worker wake-up latency is up to one second

> **Status: ✅ Done** — `queue.notify_work()` / `queue.wait_for_work()` replace the 1 s poll.
> Workers block on an `asyncio.Event` and are woken by enqueue, completion, retry and cancel
> (the last two free a concurrency slot), with a 1 s timeout as a safety net.
> Covered by `test_wait_for_work_wakes_up_on_enqueue`.

Each worker polls with `await asyncio.sleep(1)` when the queue is empty ([core/worker.py:386](../core/worker.py)). Signal an `asyncio.Event`/`Condition` from `enqueue()` and wait on it instead of polling.

### P1-5. Retries hold a worker slot and have no backoff

> **Status: ✅ Done** — retries now use exponential backoff with jitter
> (`_retry_delay()`: 2 s doubling to a 30 s cap, +0–1 s jitter), re-check `task.is_cancelled`
> before sleeping, and log the delay. `test_worker_retry` neutralises the constants so the suite
> stays fast.

On failure the worker retries in-place with a flat `sleep(1)` and no attempt re-check of cancellation; a flaky site therefore pins a slot for `job_max_attempts` seconds. Use exponential backoff with jitter, re-check `task.is_cancelled`, and surface retry state in `/status`.

### P1-6. Queue position is not priority order

> **Status: ✅ Done** — `queue.position()` ranks queued jobs by the same
> `(priority, created_at, id)` key that `claim_next()` uses; `DownloadManager.get_queue_position`
> is now async and delegates to it. Covered by `test_queue_position_follows_claim_order`.

[core/worker.py:320](../core/worker.py) sorts by `job_id` (insertion order), while the scheduler orders by `(priority, created_at, id)`. Users see a position that does not match reality; compute it from the same key used by `claim_next`.

### P1-7. Readiness never degrades after startup

> **Status: ✅ Done** — `/readyz` now runs `SELECT 1` on every request and evaluates probes
> registered via `register_probe()`; the bot registers a Telegram connection probe at startup.
> `tests/test_observability.py` covers healthy, DB-down and probe-failing responses.

`core/observability.py` sets components to `True` once and never re-checks them, so a lost database or dead Telegram connection still reports `/readyz` 200. Add a periodic health probe (the scheduler already ticks every second) or derive readiness from a live `SELECT 1`.

### P1-8. Startup wipes the downloads directory

> **Status: ✅ Done** — `paths.clear_staging()` deletes only `downloads/<numeric user id>/` and
> `downloads/tg_<user id>/`, keeps the directory itself and leaves any other file alone, so a
> bind-mounted volume survives restarts. Covered by `tests/test_paths.py`.

[core/bot.py:188](../core/bot.py) calls `shutil.rmtree(paths.DOWNLOADS_DIR, ignore_errors=True)` on every start. With the compose bind mount `./downloads:/app/downloads`, local files under that path are destroyed on restart. Either clear only staging dirs (`downloads/tg_*`, per-task dirs) or document this loudly in the runbook.

---

## P2 — Configuration and security hygiene

> **All P2 items below are fixed.** Status notes record what changed; the evidence text is kept
> for history.

### P2-1. Silent configuration drift

> **Status: ✅ Done** — `get_settings()` now compares `SPIDEY_*` keys from the environment *and*
> from `.env` against the settings model and logs an `Ignoring unknown SPIDEY_* settings`
> warning listing them. All the drift in the table below was fixed: the phantom
> `SPIDEY_DOWNLOADS_RETENTION_DAYS` example was removed, CI now sets `SPIDEY_TG_*`, Reddit
> credentials come from `Settings`, and `CAMOUFOX_*`, `PROXY_POOL`, `GDL_COOKIES_FROM_BROWSER`
> and the TeraBox token fallbacks were moved into `Settings` (new
> `SPIDEY_CAMOUFOX_*`, `SPIDEY_PROXY_POOL`, `SPIDEY_TERABOX_SIGN/_TIMESTAMP/_LOGID`). Missing
> options were added to `.env.example`, and `tests/test_config.py` covers the warning.

`Settings` uses `extra="ignore"` ([core/config.py:26](../core/config.py)), so a misspelled `SPIDEY_*` variable is discarded without a warning. Drift already present:

| Documented / assumed | Reality |
|:--|:--|
| `SPIDEY_DOWNLOADS_RETENTION_DAYS` in `.env.example` | no such setting |
| `SPIDEY_API_ID`, `SPIDEY_API_HASH`, `SPIDEY_BOT_TOKEN` in `.github/workflows/ci.yml` | real names are `SPIDEY_TG_*` |
| `SPIDEY_GDL_REDDIT_CLIENT_ID/SECRET/REFRESH_TOKEN` | `downloader/gallerydl.py` reads unprefixed `GDL_REDDIT_*` |
| `SPIDEY_*_CONCURRENT_MODE`, `SPIDEY_DEFAULT_*` | missing from `.env.example` |
| `CAMOUFOX_MAX_CONCURRENCY` | read via `os.environ` in `handler/bypass.py`, bypassing `Settings` |

**Fix:** log (or fail) on unknown `SPIDEY_*` keys at startup, and route every env read through `Settings`.

### P2-2. Database credentials hardcoded as defaults

> **Status: ✅ Done** — `database_url` is now a required field (`Field(min_length=1)`), so a
> missing or empty value fails fast instead of silently connecting to a dev database. A new
> `tests/conftest.py` supplies a placeholder URL for the suite; compose/CI already inject the
> real one.

[core/config.py:35](../core/config.py) defaults `database_url` to a `spidey:spidey@localhost:5432/spideybot` URL, mirrored in `docker-compose.yml` (which also publishes `5432`). Make `database_url` a required field and inject credentials from the environment / compose secrets.

### P2-3. SSRF validation is inconsistent

> **Status: ✅ Done** — both validators moved to [`utils/urls.py`](../utils/urls.py) as
> `validate_public_url()` / `is_obviously_private_request()`; `/bypass` imports them (no
> behaviour change) and `Handler._enqueue_download()` now rejects private, loopback, link-local,
> credential-bearing and non-HTTP(S) links before quota is touched, replying with the reason.
> Covered by `tests/test_urls.py`. Redirects followed inside yt-dlp/gallery-dl remain outside
> the check — documented as residual risk in the README security notes.

`handler/bypass.py` implements a solid `_validate_public_url` (rejects loopback, private, link-local, reserved, credentialed URLs, and re-checks browser subrequests). The gallery-dl, yt-dlp, cyberdrop-dl, TeraBox and per-site adapters accept any user-supplied URL with no equivalent check.

**Fix:** extract the validator into `utils/` and apply it to every `/dl` link in `DownloadManager.add_task` (before enqueueing) and in the per-site adapters that follow redirects.

### P2-4. Session storage claims more protection than it provides

> **Status: ✅ Done** — documented instead of re-encrypting (file sessions stay plaintext by
> design so Telethon can use them): README gained a "Security notes" section, the
> `.env.example` key comment now states that only cookies and *legacy* string sessions are
> encrypted, and OPERATIONS.md explains how to rotate a file session.

Sessions are plaintext Telethon SQLite files under `user_sessions/` (bind-mounted to the host). Only *legacy* string sessions were encrypted, yet `SPIDEY_SESSION_ENCRYPT_KEY` and the docs read as if all sessions are encrypted. Either document the distinction or encrypt at rest (e.g. `SQLCipher`/Fernet-wrapped blob).

### P2-5. Empty tracked runtime config

> **Status: ✅ Done** — `config/yt-dlp.conf` was removed from the repository (the downloader
> creates it on demand, and `/app` is owned by the app user in the image); `.gitignore` now
> ignores it together with `config/gallery-dl.json`. Only the `.example` templates are tracked.

`config/yt-dlp.conf` is committed empty and is recreated at runtime by `_ensure_ytdlp_config`. Drop it from the repo (keep `.example`) or populate it.

---

## P3 — Documentation, tests and repository hygiene

> **All P3 items below are fixed.** Notes record what changed; the evidence text is kept for
> history.

### P3-1. Docs describe a code base that no longer exists

> **Status: ✅ Done** — README and ARCHITECTURE rewritten against the real tree (no `engines.py`,
> `handler/user.py` or `handler/outgoing.py`; correct module lists, tier table and config
> block). The README no longer claims "148 tests" or points at `tests/test_handler.py`, and its
> local quick start uses `uv sync --frozen`. `PLAN.md` / `STRATEGY.md` carry a "historical"
> banner. The in-bot `/dt` help text now matches reality, the dead `/upgrade` copy and hardcoded
> upgrade button were removed, and a `LICENSE` file was added to back the README's MIT claim.

`README.md`, `docs/ARCHITECTURE.md`, `docs/PLAN.md` and `docs/STRATEGY.md` are still "V4" plans, while the code is the V3 layout:

- Referenced but missing: `handler/user.py`, `handler/outgoing.py`, `downloader/engines.py`, `tests/test_handler.py`.
- README: "148 tests" (there are 11); Premium concurrency 8 (configured 5); no monthly quota dimension exists although the tier table lists one.
- `/dt` is documented as "Download and auto-split into 1 GB chunks" ([handler/handler.py:76](../handler/handler.py)) but actually downloads a Telegram message via the user session.
- `/upgrade` is advertised in `/start` and `/help` ([handler/handler.py:58](../handler/handler.py), [:360](../handler/handler.py)) but no handler is registered for it.
- README claims MIT, but there is no `LICENSE` file.
- Local quick start uses `python -m venv` + `pip`, while CI/Docker use `uv`.

### P3-2. Test coverage collapsed

> **Status: ✅ Done (coverage restored where it matters most)** — the suite went from 11 to 47
> tests. New coverage: quota admission/limit/bandwidth + refund semantics and referral credit
> (PostgreSQL), shared-cookie tier filtering, queue pruning/wake-up/priority position, token
> bucket, readiness probes (200/503), staging cleanup, SSRF validation, unknown-config-key
> detection, the handler's pre-queue guards (`/dl` URL rejection, rate limit) and the worker
> happy path (quota consumption + referral credit + completion). Per-site adapters and the
> download flows remain untested — that needs HTTP fixtures and is the next milestone.

Commit `f0ed9dc` ("V4: rewrite for scale…") deleted the previous suite together with `tests/conftest.py`; only six top-level test files remain. There is no coverage of quota admission/refund, referral crediting, handler command flows (`/dl`, `/cancel`, `/status`), or `prepare_media`/`make_video_thumb`. Highest-value additions: `_enqueue_download` quota path, `queue` pruning behaviour, referral crediting idempotency, and the worker happy path.

### P3-3. No dependency lockfile

> **Status: ✅ Done** — `uv.lock` (106 packages) is committed and `uv lock --check` is clean.
> CI installs with `uv sync --frozen --extra test --python 3.13` (setup-uv with caching), and the
> Dockerfile installs `uv export --frozen` requirements before the project, so the image uses the
> same pins. Tool versions are pinned in the `test` extra, so a ruff/pyright release can no
> longer break the build silently.

Neither CI nor the Docker build pins transitive versions (`pip install ".[test]"`, `uv pip install .`), so builds are not reproducible — an unpinned `ruff` is what broke the pipeline. Commit `uv.lock` and use `uv sync --frozen` / `uv pip install --frozen`.

### P3-4. Stale build and cache artifacts inside the working tree

> **Status: ✅ Done** — removed the leftover `app/`, `telegram/`, `build/`, `dist/`,
> `spideybot.egg-info/`, all `__pycache__` directories, the empty `tests/{acceptance,fakes,
> integration,unit}` and `downloader/{bypass,sites}` folders, and the tool caches. `data/`
> (local PostgreSQL + bot session) was left untouched because it holds live state; moving it
> outside the repository remains a manual step.

`app/`, `telegram/`, `build/`, `dist/`, `spideybot.egg-info/`, and `tests/{unit,integration,acceptance,fakes}/__pycache__` are remnants of a removed layout (several directories contain only `.pyc` files). `data/pgdata` also contains a live local PostgreSQL data directory inside the repository. Delete them, and consider moving local PG data outside the repo.

### P3-5. Minor code smells

> **Status: ✅ Done** — the unused `_session` parameter is gone from every `core/queue.py`
> function and all call sites; `handler/admin.py` no longer commits twice inside `session_scope`;
> the scheduler logs through `structlog` like the rest of the code; the orphaned "Retention"
> header in `core/config.py` was removed.

- Every `core/queue.py` public function takes an unused `_session` argument (V4 API residue) — remove it.
- `handler/admin.py` opens `session_scope()` (which already commits) and then calls `await session.commit()` explicitly.
- `core/scheduler.py` uses stdlib `logging` while the rest of the code base uses `structlog`.
- `core/config.py` keeps a dangling `# ── Retention ──` section header above unrelated bot-behaviour defaults.
- `README`/`ARCHITECTURE` cite `metrics.py`/`observability.py` split that partially exists — verify after the doc rewrite.

---

## Suggested order of work

1. ~~**Unbreak the pipeline** — P0-1 (`Path`), P0-6 (ruff/pyright config, pinned tools), then make `main` green so tests gate merges.~~ ✅ Done
2. ~~**Make advertised features real** — P0-2 (rate limiting), P0-3 (referrals), P1-1 (`/dt` client).~~ ✅ Done — `/upgrade` copy is still advertised without a handler; tracked under P3-1.
3. ~~**Stability** — P0-4 (queue pruning), P0-5 (blocking ffmpeg), P1-2/P1-3 (TeraBox pool + N+1), P1-4/P1-5 (wake-up + backoff), P1-6 (queue position), P1-7 (readiness), P1-8 (staging cleanup).~~ ✅ Done
4. ~~**Configuration hardening** — P2-1 … P2-5.~~ ✅ Done
5. ~~**Docs, tests, lockfile** — P3-1 … P3-4, once the code above has settled.~~ ✅ Done

## Verification checklist for future audits

```bash
uv sync --frozen --extra test   # environment matches uv.lock
uv run ruff check .             # must be clean
uv run pyright                  # advisory today (baseline above); must not regress
uv run pytest -q                # must be green
uv run python -m compileall -q core handler downloader tests migrations utils main.py
docker compose config           # compose interpolation sanity
```

All four must pass locally before pushing, since the CI gate currently cannot report on them.

## Related documents

- [ARCHITECTURE.md](./ARCHITECTURE.md) — current system design.
- [SEND_RATE_LIMITING.md](./SEND_RATE_LIMITING.md) — pacing of media preparation and sends, and
  `FLOOD_WAIT` handling across concurrent jobs. ✅ Done (phase 1: `core/sendgate.py`).

