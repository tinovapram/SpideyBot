# SpideyBot — Implementation Plan (Kurigram engine)

**Input.** [FUNCTIONAL_SPEC.md](./FUNCTIONAL_SPEC.md) only. This is a **brand-new project**: no code,
layout, naming or behaviour is carried over from any other branch. `V3`/`V4`/`V5`/`main` are out of
bounds as design input.

**Engine.** [Kurigram](https://github.com/kurigram-org/kurigram) `2.2.x` — an actively maintained
Pyrogram fork (import name is still `pyrogram`). Chosen because SpideyBot needs **both** identities in
one process: a Bot-API client *and* per-user MTProto clients (FR-4 message ranges, FR-5 outgoing magic
word, FR-6 `media_unread` inbox). Kurigram supplies both, is async-native, and persists user sessions
as files.

**Status.** Planning complete, spec realigned (§11), implementation not started. Branch: `V2.0`.

---

## 0. Verified environment facts

Checked rather than assumed; these shape the plan.

| Fact | Value | Consequence |
|---|---|---|
| Kurigram release | `2.2.26`, pure-Python wheel, `requires_python >=3.10`, classifiers to **3.14** | Installs on any modern CPython; 3.14 is officially supported |
| `tgcrypto` (the `kurigram[fast]` AES accelerator) | latest `1.2.5`, released **2022-11-11**; no wheel for cp312/cp313/cp314 and no `abi3` wheel; `requires_python ~=3.7`, ships an sdist of **plain C** (not Cython) | pip falls back to a **source build** on 3.12+. Status below |
| `tgcrypto` build on 3.14 | **Verified working**: `cl /c` → `link /DLL` → `import` → IGE and CTR round-trip all succeeded against 3.14.6 + MSVC Build Tools 2026 | Fast AES on 3.14 is viable; no second interpreter needed |
| Kurigram without `tgcrypto` | Verified: logs *"will work the same, but at a much slower speed"* and falls back to pure-Python `pyaes`. **Benchmarked here**: AES-IGE over 8 MiB runs at **171.4 MiB/s** with `tgcrypto` vs **0.59 MiB/s** with `pyaes` — **291× slower** | The fallback is not viable: one 2 GiB transfer would cost ~58 minutes of CPU on AES alone. `tgcrypto` is a hard requirement, not an optimisation, so a missing accelerator must be reported rather than silently tolerated |
| Installed interpreters | only CPython **3.14.6**, plus `uv` | 3.14 is the target runtime |
| `.venv` | **recreated in batch 1**: Python 3.14.6 with the full requirement set, `tgcrypto` compiled from its sdist, plus `ruff` | The planned runtime works; `uv` builds and manages it |
| `.gitignore` | **was deleted during planning, recreated in batch 0**. `git check-ignore -v` now matches both `.env` and `data/` | ✅ Closed — a `git add -A` can no longer stage the bot token, API hash or TeraBox cookies |
| Container runtime | Docker Desktop installed, `com.docker.service` **Stopped** | The compose provider needs Docker Desktop started |
| Native PostgreSQL | none — no install, no service, no `psql` on PATH | The bundled-binaries provider is the no-Docker path |
| `postgresql-binaries` | `18.6.0`, ships **`py3-none-win_amd64`** (Python-agnostic → 3.14 OK); contains `initdb`, `pg_ctl`, `postgres` (9.8 MB), `psql`, `createdb`, `pg_isready` + `LICENSE`; `__init__.py` exposes `bin()` | Powers local Postgres with no Docker and no installer |
| `pgserver` | Windows wheels stop at **cp312**, no sdist | **Rejected** — cannot run on 3.14 (same ceiling as tgcrypto, without the escape hatch) |
| Kurigram client identity parameters | real signature is `device_model`, `system_version`, `app_version`, `lang_code`, `lang_pack`, **`client_platform`** — the docs' sample tables call the last one `platform` | The identity mapper translates `platform` → `client_platform`; an unrecognised platform is dropped rather than raising over a cosmetic field |
| Kurigram smart plugins | `load_plugins()` is invoked from **`start()`**, not `__init__`, and adds each handler through `loop.create_task` | Handlers exist only once a loop has run: a client built and never started advertises zero handlers, and the client captures whichever loop was current when it was constructed |
| Event loop on 3.14 | `pyrogram.utils.get_event_loop()` catches the `RuntimeError` 3.14 raises when no loop is current, then creates one | `client.run()` works on 3.14; `uvloop.install()` must still run first so the new loop comes from the patched policy |
| `logging.LogRecord` on 3.14 | a lone mapping argument stays as `record.args` (the dict itself) and is **no longer merged onto the record** | The redacting filter scrubs that mapping directly; a filter written for the older merge behaviour would miss it |
| Kurigram + tgcrypto on 3.14, live | verified against production: bot connected to `Production DC5`, both handlers loaded, session reported `Device: Desktop - 6.0.2 x64` / `System: Windows 11 x64 (en)` | The identity profile (D16) reaches Telegram for real, not just the config object |
| YouTube format availability | probed against a real video: **0 of 49 formats are single-file** (37 video-only, 12 audio-only, highest video 2160p), and yt-dlp's default selection is the merge `401+251` (AV1 video + Opus audio) | ffmpeg is required for the YouTube feature to *exist*, not to raise its quality. The default pick is also a compatibility trap — see D28 |
| ffmpeg on this machine | `9.0.1-essentials` (winget) on `PATH`, under a **version-stamped** directory, with `ffprobe` alongside | Resolve with `shutil.which`, never a fixed path, and never vendor the binaries (licensing + size) |

**Risk R1 (revised after testing — see §8):** fast AES depends on a *source build* (toolchain needed in
CI/Docker) from a package last released in 2022. It builds cleanly on 3.14 today, and `pyaes` remains
the automatic fallback.

---

## 1. Decisions locked (answers to spec §12)

| # | Topic | Decision | Rationale |
|---|---|---|---|
| D1 | Runtime / framework | Python **3.14**, asyncio, single process, **Kurigram 2.2.x**, with a **C toolchain required** (MSVC Build Tools on Windows, a `gcc` stage in Docker) | Kurigram classifies 3.14 as supported, but `tgcrypto` has no wheel above cp311, so 3.14 means compiling it. Chosen over pinning 3.11 — that would only defer the compiler requirement while locking in an older runtime — because the `pyaes` fallback measured 291× slower and so is not a fallback at all |
| D2 | Persistence | **PostgreSQL** — SQLAlchemy 2.0 async + `asyncpg` + Alembic | Enables `FOR UPDATE SKIP LOCKED` leases (FR-10.5) and atomic quota upserts (FR-3.2) |
| D3 | Provisioning | **Bring-your-own-Postgres.** The app never knows where Postgres lives: `[db]` host/port/name/user + `SPIDEY_DB_PASSWORD` is the only coupling. Three equally documented providers — bundled binaries, compose, existing/remote | Direct run without Docker is first-class; Docker is one provider, not a dependency |
| D4 | **No SQLite backend** | Postgres only | The queue depends on `FOR UPDATE SKIP LOCKED`; SQLite would silently change concurrency semantics and break "exactly once" (§8.2/E26) in the environment where it would go unnoticed |
| D5 | Extraction | **gallery-dl is the only external extractor.** Native adapters run first; everything else goes to gallery-dl with `extractor.ytdl.enabled = true`, so yt-dlp-backed sites resolve through it. `yt-dlp` stays *installed* because gallery-dl imports it lazily, but our code never invokes it | One invocation surface — one config, one cookies/proxy/retry path — instead of two parallel stacks. Note the real flow: gallery-dl's `ytdl` extractor resolves, and its `ytdl` **downloader** would fetch via yt-dlp; we override that by downloading ourselves (D25) |
| D6 | Adapter scope | **Engine-first.** Engines cover the broad platform list; native adapters only where they fail: **TeraBox**, **Cyberdrop family**, and the **ad-gated hosts** (Doodstream, StreamTape, MixDrop, StreamWish, Vidmonstr, Vidara, Luluvdoo, Bysejikuar, MEGA) | Each native adapter is a module + smoke test + a standing breakage surface (R4); breadth is free from the engines, so reliability is bought only where needed |
| D7 | **Sessions storage** | Kurigram native **File Storage**, **plaintext**: `data/sessions/<user_id>.session` | Operator decision. Simplest and self-healing; no key management. Accepted risk documented in §8/R10 |
| D8 | **Cookie storage** | Plaintext file: `data/cookies/<user_id>` | Mirrors D7 for consistency |
| D9 | **Encryption** | **None anywhere in the system.** No Fernet, no key management, no `user_secrets` table, and `SPIDEY_SESSION_ENCRYPT_KEY` is dropped | Follows from D7/D8 |
| D10 | Revocation | `/logout` calls `log_out()` **(server-side)** and *then* deletes the file | Deleting only the file leaves the auth key valid and the session listed in Active Sessions — the spec requires genuine revocation |
| D11 | Config layering | Committed `config/default.toml` (all §13 defaults as a reviewable `[tier.*]` table) → optional gitignored `config/local.toml` → `SPIDEY_*` env vars (highest) | Defaults become reviewable in a diff; TOML sources can be validated with `extra="forbid"` so a typo fails loudly instead of silently defaulting |
| D12 | Secrets | Bootstrap secrets only (bot token, API hash, DB password, TeraBox/Reddit creds) via a `SecretsProvider` seam; env today, keyring/Docker secrets later | Keeps non-secret config separable and makes an OS secret store a drop-in |
| D13 | Structure | **Shallow layers**: `main.py`, `app/`, `handlers/`, `download/`, `telegram/`; nothing deeper than two levels, no `src/` nesting | Operator decision |
| D14 | Handler registration | Kurigram **smart plugins** — `plugins=dict(root="handlers", include=[...])` with `@Client.on_message` decorators | Removes the manual registration layer entirely. Caveat: one handler per group per update, so commands, the magic word and TimerMedia need separate groups |
| D15 | Queue / workers | Single process; Postgres priority queue with leases, written for `SKIP LOCKED` from day one | Multi-worker later becomes a config change, not a rewrite |
| D16 | Client identity | Configurable `[client] profile = "desktop" \| "android" \| "custom"` for **user clients** (`device_model`, `system_version`, `app_version`, `lang_pack`, `lang_code`, `client_platform`) | Account-safety, not cosmetics: a "CPython / Kurigram" identity in a user's Active Sessions raises the odds of their account being actioned |
| D17 | Speedups | `tgcrypto` installed unconditionally; `uvloop` a guarded **Unix-only** optional install | `uvloop`'s own wheel markers exclude Windows, so Windows dev runs without it |
| D18 | Scheduling | Hand-rolled asyncio scheduler with an **injected deterministic clock**; APScheduler not adopted | The test plan needs a controllable clock for the daily reset and 30-day bonus window |
| D19 | Live testing | Opt-in `@pytest.mark.live` suite driven by Kurigram **`test_mode=True`** test servers | Exercises login, 2FA, session lifecycle and triggers against test DCs with throwaway accounts |
| D20 | Admin elevation | Command-only (`/addpremium`, `/removepremium`, `/checkpremium`) with inline confirmations; grant rows carry expiry | FR-19, FR-2.2 |
| D21 | Cleanup / retention | Per-job temp dir removed on every terminal state; startup orphan sweep; expired leases re-queued; idle user clients reaped (idle timeout + LRU + live cap); retention window for history | FR-10.5, E26, NFR-3 |
| D22 | Rate limiting | Per-user submission token bucket; `FloodWait` → sleep + single retry (reporting seconds during `/login`); per-host exponential backoff with jitter | FR-7.7, E8, E14, NFR-2 |
| D23 | Localization | Not in v1. All user-facing copy in one catalog module keyed by id, preserving spec §7 wording | Later i18n becomes a data change |
| D24 | File transfer | Stream to a per-job temp dir, upload with progress callbacks, group ≤ `album_size` (10) per media group, flush remainder, delete the temp dir on completion | FR-8, NFR-3 |
| D25 | Byte transfer | **We fetch the bytes ourselves** (`curl_cffi`/`aiohttp`) from the media URLs gallery-dl resolves, rather than letting gallery-dl's downloader do it | Uniform byte-level progress for FR-7, our own size accounting for the quota, and the E3 pre-download size gate operates on resolved sizes. Costs are tracked in R13 |
| D26 | Extraction config | A committed `config/gallery-dl.json` enables `extractor.ytdl` and pins `extractor.ytdl.websites` as an allowlist, so the generic yt-dlp fallback cannot swallow URLs our native adapters should own | Enabling ytdl makes the generic extractor match *any* otherwise-unhandled URL; the allowlist keeps that from pre-empting TeraBox and the ad-gated hosts |
| D27 | Required capabilities | `tgcrypto`, `ffmpeg` and `ffprobe` are **required capabilities**: probed at startup (a missing one is logged at ERROR), exposed by `spideybot check` (non-zero exit), and consulted by the code that needs them so a job fails with a reason rather than degrading | Starting silently is exactly what makes a 291× slowdown, or a download with no audio, hard to diagnose. Chosen over refusing to start because hosts that need neither still work |
| D28 | Media selection | Compatibility-first: prefer H.264 + AAC in MP4, using the best available stream (AV1/Opus) only when nothing compatible exists. **Proposed — confirm at batch 3a** | YouTube's own default was AV1 video + Opus audio; Telegram playback of AV1 is inconsistent, and a file that will not play is not a successful download |

---

## 2. Architecture

### 2.1 Process model

```
                 ┌──────────────────────────── asyncio process ────────────────────────────┐
                 │                                                                         │
  Bot API ──────▶│  BotClient (kurigram, bot_token)                                        │
                 │      │  on_message / on_callback_query                                   │
                 │      ▼                                                                  │
                 │  handlers/ (smart plugins)  ──▶  app/ (tiers·quota·referral)             │
                 │      │                                 │                                │
                 │      │  enqueue                        ▼                                │
                 │      └──────────▶  app/queue (Postgres, priority + lease)                │
                 │                           │                                             │
                 │  user clients (session files) ◀── workers (N asyncio tasks)              │
                 │      ▲   ▲                │                                             │
                 │      │   │                ▼                                             │
                 │  handlers/  │        download/flow ──▶ download/engines + download/sites │
                 │  magic_word │             │              + download/bypass               │
                 │  timer_media│             │                                             │
                 │             └─────────────┴──▶ telegram/delivery → albums → user chat    │
                 │                                                                         │
                 │  app/scheduler  (daily reset · lease sweep · temp sweep · client reaping)│
                 └─────────────────────────────────────────────────────────────────────────┘
                          │                                  │
                 PostgreSQL (users·quota·jobs·                temp files under
                 referrals·prefs·stats)                       data/tmp/<job_id>/
                 data/sessions/ · data/cookies/ (plaintext files)
```

### 2.2 Package layout

Nothing deeper than two levels; no `src/` nesting.

```
SpideyBot/
  main.py                  # entrypoint: Client(...) + plugins=dict(root="handlers")
  app/                     # infrastructure + domain
    config.py  logging.py  paths.py
    db.py  models.py
    tiers.py  quota.py  queue.py  scheduler.py
    sessions.py  cookies.py  referral.py  metrics.py
  handlers/                # Kurigram smart plugins, decorator-registered
    start.py  help.py  download.py  tg_download.py  cancel.py  job_status.py
    account.py  login.py  setting.py  quota_info.py  sites.py  referral.py
    bypass.py  admin.py  magic_word.py  timer_media.py
  download/
    engines.py  flow.py  probe.py  registry.py
    sites/                 # native adapters only: terabox.py, cyberdrop_family.py, ad-gated hosts
    bypass/                # FR-18 shortener families
  telegram/
    clients.py  status.py  delivery.py  progress.py  keyboards.py
  tests/                   # unit/, integration/, acceptance/, fakes/
  migrations/              # Alembic
  config/                  # default.toml (committed), local.toml (gitignored)
  data/                    # runtime, gitignored: pgdata/, sessions/, cookies/, tmp/, downloads/
  pyproject.toml  requirements.txt  .env.example  .gitignore
  Dockerfile  docker-compose.yml
```

Handler filenames deliberately avoid colliding with domain modules: the `/status` command is
`handlers/job_status.py` (the status-message view is `telegram/status.py`) and `/quota` is
`handlers/quota_info.py` (the engine is `app/quota.py`). Without that, a search for `status` or `quota`
returns two unrelated files.

**Boundary rule:** handlers never touch the DB or the extractors directly. They call `app/` facades and
enqueue jobs; the pipeline is reachable only through `app/queue`. This is what makes AC-1…AC-15
testable without a live Telegram connection.

### 2.3 Kurigram-specific design notes

| Concern | Approach |
|---|---|
| Bot identity | `Client(name, api_id, api_hash, bot_token=…)`; handlers via `@Client.on_message(...)` / `@Client.on_callback_query(...)` |
| Per-user identity | `Client(f"data/sessions/{user_id}", api_id, api_hash, …)` — file storage, so restart reconnects with no login |
| Command parsing | `filters.command(..., prefixes="/", case_sensitive=True)` gives whole-word matching **and** the `/cmd@BotName` form for free (§4 parsing rules) |
| `/start <payload>` | `message.command[1]`; absent/non-numeric/self payloads ignored silently (FR-1.2, E21, E22) |
| Status edits | `edit_text`; `MessageIdInvalid` → recreate the message (E7); `FloodWait` → sleep + retry, coalesced by `telegram/status.py` (FR-7.7, E8) |
| Albums | `InputMediaPhoto/Video/Audio/Document` lists → `reply_media_group`; caption on the first item (FR-8) |
| Progress | `progress=` callbacks on download/send calls → `telegram/progress.py` → status rows (FR-7.4/7.5) |
| `/dt` ranges | `get_chat_history` over the parsed id range; counts scanned messages, reports source chat title (FR-4.5) |
| Magic word | user-client handler on `filters.private & filters.outgoing`, single-token match, own group (FR-5.3, D14) |
| TimerMedia | user-client handler on `filters.private & filters.incoming` requiring photo/video + `media_unread`, own group (FR-6.2, D14) |
| Login flow | phone → code → optional 2FA on a pending user client; `SessionPasswordNeeded`, `PhoneCodeInvalid`, `PhoneCodeExpired`, `FloodWait` mapped to spec §7.6 copy |
| Ban on old engines | Importing `telethon` or `cryptg` anywhere is a lint failure — they must never leak in |
| Support modules | `python-socks[asyncio]` and `pyaes` arrive with Kurigram; `tgcrypto` is **required**, not optional (D1) |
| Runtime plugin control | Each decorated handler exposes `func.handlers` as `(handler, group)` tuples, so `remove_handler()` can unload a plugin without a restart — the basis for a maintenance kill-switch (e.g. disabling `/bypass` during an incident) |
| Session escape hatch | `export_session_string()` is held in reserve: it is how a session would be backed up, migrated between hosts, or moved to a DB-backed store if R11 ever bites |
| Test-mode namespace | Live tests use `data/sessions/test/<user_id>.session`, so a test run can never clobber a real user's session |

### 2.4 Adopted from the Kurigram documentation

| Doc page | What we adopt |
|---|---|
| client-settings / decreasing ban | The verbatim presets in §6 are used for user clients (D16). Two cautions from the page: **do not ship Telegram's own app credentials** (`api_id=6` / `2040` in their snippets belong to Telegram's official clients) — we keep the operator's `my.telegram.org` pair; and their `lang_code="jabka"` is a placeholder, not a real ISO 639-1 code, so we default to `en` |
| speedups | `tgcrypto` is **required** and checked at startup, because a missing accelerator is 291× slower (D1/R1); `uvloop` guarded and Unix-only (D17). **Ordering matters:** `uvloop.install()` must run before `asyncio.run()` *and* before any `Client` is constructed, or it silently has no effect — done in `main.py` inside `try/except ImportError` |
| smart-plugins | Decorator-based registration from `handlers/` with an explicit `include` list for deterministic order (D14); per-module order can be pinned with the `"module fn3 fn1 fn2"` syntax; runtime unload via `remove_handler()` (§2.3). Overlapping filters must sit in different groups — "one handler per group, per update" is enforced by design |
| storage-engines | Native File Storage for sessions (D7). `in_memory=True` is used for throwaway clients so the live suite leaves no session files behind; `export_session_string()` is held in reserve for backup/migration (§2.3); the `Storage` ABC is the documented extension point should files ever need encrypting |
| scheduling | APScheduler available but not adopted; a hand-rolled scheduler keeps the injectable clock (D18) |
| test-servers | `test_mode=True` with reserved numbers `99966XYYYY` (X = DC 1–3) whose **login code is computable** — the DC digit repeated five or six times — so the login flow can be automated with no phone. Running **two** test-account clients against each other makes AC-7 fully automatable on test DCs (message, channel and range links with captions), because the chat content is ours to create (D19) |

---

## 3. Data model & on-disk state

PostgreSQL holds everything except credentials, which are plaintext files (D7/D8).

```
users(id PK, username, first_name, tier, tier_expiry, is_admin,
      onboarded_at,            -- FR-1.5/1.6 "already started", survives restarts
      referred_by,             -- denormalised for first-start-wins
      created_at, updated_at)

tier_grants(id, user_id FK, tier, granted_by, granted_at, expires_at, revoked_at)   -- FR-2.2, FR-19

usage_daily(user_id, day, downloads, bonus_downloads, bytes_in, bytes_out,
            PRIMARY KEY(user_id, day))                                                -- FR-15, §8.5

jobs(id BIGSERIAL, user_id FK, link, site, tier, priority, status, route,
     attempts, total_bytes, resolved_count,
     claim_token UUID, lease_expires,            -- restart-safe lease (FR-10.5)
     status_chat_id, status_message_id,          -- the single status message (FR-7.1)
     created_at, started_at, finished_at, cancel_requested_at)

job_items(id BIGSERIAL, job_id FK, idx, source_url, filename, media_kind,
          bytes_total, bytes_done, state, reason, uploaded_message_id,
          UNIQUE(job_id, idx), UNIQUE(job_id, source_url))                        -- FR-8.5, FR-9, E6

referrals(id, referrer_id FK, invitee_id FK UNIQUE, status, credited_at, created_at)  -- FR-17.6
referral_bonuses(id, referrer_id, invitee_id, credited_at, expires_at)                -- +10/day for 30d

user_prefs(user_id PK, magic_word, timer_media, locale, updated_at)                   -- FR-5.2, FR-6.4

stats_daily(day, submitted, delivered, failed, cancelled, files_sent, bytes_up)       -- FR-19 /stats
```

**No `user_secrets` table** (D9). On-disk instead:

```
data/sessions/<user_id>.session    # Kurigram SQLite session, plaintext (D7)
data/sessions/test/<user_id>.session  # live-test sessions, kept separate so a test run
                                      # can never clobber a real user's session (D19)
data/cookies/<user_id>             # TeraBox cookie, plaintext (D8)
data/pgdata/                       # local Postgres cluster, bundled-binaries provider
data/tmp/<job_id>/                 # per-job staging, deleted on terminal state
```

Session state for FR-13 badges is derived from file presence plus the live-client registry: no file →
🔴 No session; file present, client stopped → 🟡 Session saved (not running); live client → 🟢 Connected.

Design notes:

- **One status message per job** is a *stored* pair (`status_chat_id`, `status_message_id`) so a
  restarted worker rebinds to the same message instead of spamming a new one (FR-7.1, E7).
- **`job_items` is the idempotency ledger** (FR-8.5, FR-9.1, E26): `state ∈
  pending|downloaded|uploaded|failed`, and `files_sent` derives from it, so a resumed job never
  re-uploads what already landed.
- **Quota is one row per user-day** → atomic `INSERT … ON CONFLICT DO UPDATE` (FR-3.2, §8.5).
- **Leases, not locks held across awaits** → claim, renew, requeue on expiry (D15, E26).

---

## 4. Phases

Each phase is independently shippable with a *done when* that a command can verify. AC ids are spec §11.

### P0-prereq — DB provider (Docker-free)
- [x] `app/paths.py`; `spideybot db` CLI: `init | start | stop | status | migrate | reset` over
      `postgresql-binaries` — exercised: `init`, `start`, `status`, `migrate`. `reset` is implemented
      but deliberately not exercised, since it destroys the cluster
- [x] Loopback-only, non-default port, `data/pgdata/` gitignored, idempotent `start`, graceful stop
- [ ] Layered test DSN resolver: `SPIDEY_TEST_DATABASE_URL` → bundled cluster → compose service

**Done when:** `python main.py db start` boots Postgres with Docker stopped, and the integration
suite passes both with and without Docker. — **Half met:** the DB half is verified (Docker stopped,
`initialised/running/ready`, migrations applied and idempotent). The integration suite arrives with
the queue in batch 2, since there is nothing to integrate until then.

### P0 — Foundations
- [x] Recreate `.gitignore` **first** (`.env` is currently unignored — see §0)
- [x] Fresh Python 3.14 venv via `uv`; `uv pip install -r requirements.txt`, plus `ruff`.
      `cryptography` is absent because D9 encrypts nothing, and `camoufox` is deferred to batch 5 per
      §12. `tgcrypto` compiles from the sdist (verified) and is now a **required** capability (D27)
- [x] `config/default.toml`, `config/local.toml.example`, `.env.example`; `git check-ignore` confirms both
      `.env` and `data/`
- [x] `app/config.py` — 4-layer chain, `extra="forbid"` on TOML, `SecretStr`, `SecretsProvider`
- [x] `app/logging.py` (secret redaction), `app/models.py`, Alembic baseline (nine tables, applied)
- [x] `data/sessions/` and `data/cookies/` created service-account-only + startup world-readable warning
- [ ] Message catalog seeded from spec §7 — §7.1/§7.2 (onboarding, help, usage) live in `app/catalog.py`;
      §7.3–§7.8 land with the features that print them
- [x] `main.py` + `handlers/start.py` + `handlers/help.py` via smart plugins + admin guard
- [x] `app/capabilities.py` + `spideybot check`, so a missing `tgcrypto`/`ffmpeg` is never silent (D27)
- [x] First `tests/unit`: tier limits and precedence, catalog rendering (asserting no placeholder
      survives), redaction including the 3.14 mapping-argument path, filesystem layout, Kurigram wiring,
      both handlers driven directly, capabilities — 109 tests

**Done when:** the bot starts, migrates a fresh Postgres, answers `/start` and `/help`, `pytest tests/unit` green.
— **Met** (batch 1), with one caveat stated rather than hidden: Telegram routing a real user's message into
the handler was not exercised, because that needs a human to message the bot. Everything up to delivery is
covered by tests, and the live startup proved both handlers load.

### P1 — Identity, tiers, quota gates
- [ ] Onboarding state machine: registration, silent referral capture, first-vs-repeat `/start` (FR-1)
- [ ] Tier resolution with expiry revert; admin precedence (FR-2)
- [ ] Quota check / reserve / commit / release; daily boundary (FR-3.2, FR-15, §8.5)
- [ ] `/quota` with `∞` rendering; submission rate limiter (D22)

**Done when:** AC-1 and AC-10 pass.

### P2 — Queue, jobs, cancellation
- [ ] `app/queue.py`: priority + submission order, lease claim/renew/requeue (FR-10)
- [ ] Workers: per-tier slots, release on terminal state
- [ ] `/cancel` and `/status` with ownership/unknown-id refusals (FR-11, FR-12)
- [ ] Restart recovery gated on `files_sent`; delivered jobs never re-run (§8.2, E26)
- [ ] Scheduler: lease sweep, daily reset (D18)

**Done when:** AC-4, AC-5, AC-6 pass, including kill-and-restart with no re-delivery.

### P3 — Download pipeline & delivery (engine-first, D6)
- [ ] `download/contracts`, `registry`, `probe`, `flow`; host → engine by default, native override only
      for TeraBox, the Cyberdrop family and the ad-gated hosts
- [ ] Engines: yt-dlp, gallery-dl, cyberdrop-dl, generic HTTP (`curl_cffi`)
- [ ] Native adapters: TeraBox (account pool + per-user cookie), Cyberdrop family, Doodstream,
      StreamTape, MixDrop, StreamWish, Vidmonstr, Vidara, Luluvdoo, Bysejikuar, MEGA
- [ ] Fallback chain with a visible phase change (E2, §3); pre-download size gate (E3, AC-3)
- [ ] Empty share (E4); 0-byte → `empty file (0 bytes)` (E6)
- [ ] Album delivery ≤10, captions, reply-to-command, never deliver twice (FR-8)
- [ ] Failure block `⚠️ N file(s) failed:` with ≤10 bullets + remainder (FR-9, E27)

**Done when:** AC-2, AC-3, AC-15 pass.

### P4 — Live status UX
- [ ] `telegram/status.py`: header / rows / footer, phases `Resolving → Downloading → Uploading → Done`
- [ ] Animated ellipsis while a transfer is active; counters + failure count (FR-7.4/7.6)
- [ ] Coalescing at `render_interval` / `anim_interval`; never drop the final result (FR-7.7, E8)
- [ ] Deleted-status recreation (E7); replace-not-append on completion (FR-7.9)

**Done when:** AC-2 asserts phase progression on one message id; E7/E8 replays pass.

### P5 — Telegram downloads & account sessions
- [ ] `/login` phone → code → 2FA with the spec §7.6 error copy (E13/E14/E15)
- [ ] `/logout` = `log_out()` **then** delete the file (D10); `/account` badges from file presence
- [ ] Kurigram File Storage at `data/sessions/<user_id>.session`; reconnect on `/start` (§8.3)
- [ ] Client identity profile (D16); live cap + idle reaping + LRU
- [ ] `/dt` for message / channel / folder / **range** links, captions preserved, counts reported (FR-4)

**Done when:** AC-7 passes and a restart reconnects from the session file.

### P6 — Reply triggers
- [ ] Magic word: outgoing single-token match, per-user word, guidance replies, Saved Messages delivery (FR-5)
- [ ] TimerMedia: incoming private photo/video with `media_unread`, sender caption, toggle, fail-open (FR-6)

**Done when:** AC-8 and AC-9 pass, including the "changed magic word disables `WOW`" case.

### P7 — Settings, sites, bypass, referrals
- [ ] `/setting` inline menu: cookie Set / View(masked) / Delete(confirm); TimerMedia toggle; magic word (FR-14)
- [ ] `/sites` (FR-16); `/bypass` across the FR-18 families with clear failures
- [ ] Referrals: invite link, `?start=` capture, pending → credited on first successful download,
      +10/day for 30 days, self/duplicate rejection (FR-17)

**Done when:** AC-11, AC-12, AC-13 pass; no full cookie value appears in UI or logs.

### P8 — Admin, hardening, acceptance
- [ ] `/stats`, `/addpremium`, `/removepremium`, `/checkpremium` + admin-only refusal (FR-19, AC-14)
- [ ] Structured logs with redaction; temp/orphan cleanup and retention (NFR-6, D21)
- [ ] Full AC-1…AC-15 suite in CI; `pytest -m live` suite (D19)
- [ ] Multi-stage Docker image (tgcrypto build stage); README with the three-provider Postgres table —
      **the image half is done and verified** (`Dockerfile` + `.dockerignore`: `tgcrypto` compiles in the
      builder, `spideybot check` reports `ok` in the container, and no `.env`/`data/` reaches a layer).
      Still open: a compose service and the README's provider table

**Done when:** every AC in spec §11 has a named automated test and `docker compose up` runs the bot.

---

## 5. Test strategy

| Layer | Scope | Infrastructure |
|---|---|---|
| `tests/unit` | tier/quota math, referral rules, catalog rendering, album batching, status coalescing, mask helpers | none |
| `tests/integration` | queue leases, worker slots, restart recovery, repositories | Postgres via the layered DSN resolver |
| `tests/acceptance` | FR/AC behaviour through handlers | `tests/fakes/` Kurigram client implementing the same protocol as the real one |
| `tests/live` (`@pytest.mark.live`) | only the non-fakeable criteria | Kurigram `test_mode=True` test servers + real hosts |

- `pytest` + `pytest-asyncio`, `asyncio_mode = "auto"`; CI runs `pytest -m "not live"`.
- **Fake transport** is the pillar: handlers depend on a narrow protocol (`send_message`,
  `edit_text`, `reply_media_group`, `download_media`, …) with the Kurigram client as one implementation,
  so AC-1…AC-15 run in CI with no network and no accounts.
- **Injected deterministic clock** into quota, referral and scheduler so the daily reset and the 30-day
  bonus window are tested exactly (§8.5).
**Live suite** (`@pytest.mark.live`, skipped unless `SPIDEY_TEST_MODE=true`) runs against Telegram's
**test DCs** with throwaway accounts — never production — using `in_memory=True` clients so no session
files are left behind. Login, 2FA, session lifecycle, the magic word and TimerMedia are exercised by
running two test accounts against each other, and because test-DC content is ours to create, AC-7
(message, channel and range links with caption preservation) is fully automatable there. Only genuinely
real-world inputs still need production: a real public channel, a live TeraBox link, live
Cloudflare-gated shorteners (AC-13) and a real multi-file share (AC-15), plus AC-2/AC-3 at scale.
Targets come from config/env, so no credentials are ever committed.
- Naming: `tests/acceptance/test_ac_01_onboarding.py` … `test_ac_15_failure_transparency.py`.

---

## 6. Configuration & secrets

Layering, lowest to highest precedence:

```
code defaults  →  config/default.toml  →  config/local.toml  →  environment / .env
```

Secrets never appear in TOML; they are read through `SecretsProvider` (env today; keyring or Docker
secrets later, additively).

`config/default.toml` — committed, the §13 contract as reviewable data. `0` in a size field means unlimited:

```toml
[tier.free]
link_total_mb = 500
daily_downloads = 10
daily_mb = 1024
concurrent = 1
priority = 2.0

[tier.pro]
link_total_mb = 10240
daily_downloads = 50
daily_mb = 25600
concurrent = 2
priority = 1.0

[tier.premium]
link_total_mb = 0
daily_downloads = 200
daily_mb = 102400
concurrent = 3
priority = 0.5

[admin]
priority = 0.2

[client]
profile = "desktop"        # desktop | android | custom  (D16)

# Presets taken verbatim from the Kurigram docs. This identity is what a user sees in
# their own Active Sessions, so it is an account-safety setting, not cosmetics (D16).
# The docs' lang_code is a placeholder - use a real ISO 639-1 code.
[client.desktop]
device_model = "Desktop"
system_version = "Windows 11 x64"
app_version = "6.0.2 x64"
lang_pack = "tdesktop"
lang_code = "en"
platform = "DESKTOP"

[client.android]
device_model = "Samsung SM-S931B"   # device MODEL, not the marketing name
system_version = "15 (35)"
app_version = "11.13.2 (60601)"
lang_pack = "android"
lang_code = "en"
platform = "ANDROID"

[ux]
album_size = 10
failure_list_limit = 10
render_interval = 3.0
anim_interval = 1.2
default_magic_word = "WOW"
timer_media_default = true

[referral]
daily_bonus = 10
bonus_days = 30

[runtime]
max_concurrent = 20
job_lease_seconds = 300
job_max_attempts = 3
rate_limit_per_minute = 20
session_max_live = 50
session_idle_seconds = 900

[db]
host = "127.0.0.1"
port = 55432
name = "spideybot"
user = "spideybot"
# password is a secret -> SPIDEY_DB_PASSWORD

[download]
retention_days = 14
temp_root = "data/tmp"

[testing]
# Live mode itself is switched on by SPIDEY_TEST_MODE (see .env.example). It is a
# test-run concern rather than bot behaviour, so it stays out of the config file.
session_root = "data/sessions/test"
```

`config/local.toml` — optional, gitignored, machine/deployment overrides.
`.env` — gitignored, **secrets only**: `SPIDEY_TG_API_ID`, `SPIDEY_TG_API_HASH`, `SPIDEY_TG_BOT_TOKEN`,
`SPIDEY_DB_PASSWORD`, `SPIDEY_ADMIN_IDS`, TeraBox cookies/jstoken/bdstoken, gallery-dl + Reddit creds.
**`SPIDEY_SESSION_ENCRYPT_KEY` is removed** (D9). Live-test entries (`SPIDEY_TEST_MODE`,
`SPIDEY_TEST_PHONE`, `SPIDEY_TEST_PHONE_B`) are documented in `.env.example` but stay empty by default.
**An empty value must be treated as *unset*** — not as an empty host, port or mode — since the template
ships every key present-but-blank.
`.env.example` — committed, placeholders only.

Validation: TOML loaded with `extra="forbid"` so a typo'd key fails at startup; secrets typed as
`SecretStr`; numeric ranges validated; the log formatter redacts registered secret values (NFR-6). The
DB password is never embedded in a URL — the DSN is assembled at runtime from `[db]` plus the secret.

---

## 7. Requirement traceability

| Requirement | Phase | Primary module | Acceptance |
|---|---|---|---|
| FR-1 onboarding, referral capture, already-started | P1 | `app/` onboarding + `handlers/start.py` | AC-1 |
| FR-2 help | P0 | `handlers/help.py` | AC-1 |
| FR-3 `/dl` + quota validation | P1, P3 | `app/quota.py`, `handlers/download.py` | AC-2, AC-3, AC-10 |
| FR-4 `/dt` | P5 | `download/telegram_source` + `handlers/tg_download.py` | AC-7 |
| FR-5 magic word | P6 | `handlers/magic_word.py` | AC-8 |
| FR-6 TimerMedia | P6 | `handlers/timer_media.py` | AC-9 |
| FR-7 progress/status | P4 | `telegram/status.py` | AC-2, E7, E8 |
| FR-8 delivery format | P3 | `telegram/delivery.py` | AC-2 |
| FR-9 failure reporting | P3 | `download/flow.py` + catalog | AC-15, E27 |
| FR-10 queue & concurrency | P2 | `app/queue.py` | AC-4 |
| FR-11 cancellation | P2 | `handlers/cancel.py` | AC-5 |
| FR-12 `/status` | P2 | `handlers/job_status.py` | AC-6 |
| FR-13 accounts/sessions | P5 | `handlers/login.py`, `app/sessions.py` | AC-7 |
| FR-14 settings menu | P7 | `handlers/setting.py` | AC-12 |
| FR-15 `/quota` | P1 | `handlers/quota_info.py` | AC-10 |
| FR-16 `/sites` | P7 | `handlers/sites.py` | — |
| FR-17 referrals | P7 | `app/referral.py`, `handlers/referral.py` | AC-11 |
| FR-18 bypass | P7 | `download/bypass/` | AC-13 |
| FR-19 admin tools | P8 | `handlers/admin.py` | AC-14 |
| §8 state, idempotency | P2, P3 | `app/models.py`, `app/queue.py` | AC-2, E26 |
| §9 edge cases | all | distributed | E1–E28 |
| §10 non-functional | P4, P8 | `app/logging.py`, `app/paths.py` | redaction + timing tests |

---

## 8. Risks & mitigations

| # | Risk | Impact | Mitigation |
|---|---|---|---|
| R1 | `tgcrypto` has no cp312+ wheel, so fast AES must be **compiled from the sdist** (last release 2022-11-11: plain hand-written C, not Cython) | Installs need a C toolchain, and a future CPython could break the 2022 C | **Compiled from source and required, not optional** — the source build is verified on 3.14 (MSVC `cl`/`link`, import, IGE+CTR round-trip) and the live connection confirms Kurigram selects it. The `pyaes` fallback is 291× slower (0.59 vs 171.4 MiB/s), so it is not a safety net. Mitigations: MSVC Build Tools on Windows, a `gcc` builder stage in Docker (**verified** — the image compiles it and `spideybot check` reports `ok` inside the container), a **startup check** that reports a missing accelerator, and a pin plus smoke test whenever CPython moves |
| R2 | Telegram flood limits on frequent status edits | Status errors, stalled jobs | Coalesced editor + `FloodWait` sleep/retry; the final result is always sent (FR-7.7, E8) |
| R3 | Per-user clients are expensive (socket + crypto state each) | Memory/connection exhaustion | Live cap + idle reaping + LRU (D21); sessions live on disk, clients start on demand |
| R4 | Third-party hosts drift or add anti-bot gates | Downloads break silently over time | Registry + layered fallback (D5); per-engine end-to-end tests; camoufox for gated hosts |
| R5 | TeraBox account pool blocked or limited (E16) | TeraBox failures | Round-robin pool, skip-on-block, clear reason including account count |
| R6 | Multi-hour jobs across a restart | Duplicate delivery or lost work | Leased queue + `job_items` ledger; delivered jobs never re-run |
| R7 | Windows dev vs Linux prod (uvloop absent, path/case sensitivity) | "Works on my machine" | `app/paths.py`; uvloop optional and guarded (D17); CI on Linux, dev on Windows |
| R8 | **Users' accounts actioned** because the client identity looks non-official | Severe — third-party account loss | Configurable client profile mimicking a real client (D16); document the trade-off; `/logout` offers genuine revocation |
| R9 | Scope creep from spec §14 (billing, web UI) | Bloat | Explicitly excluded; tiers granted administratively only |
| R10 | **Plaintext session/cookie files** (D7–D9) | A copied file is a logged-in device, no password needed | Files restricted to the service account; startup warning if world-readable; one file per user; never logged or exported; gitignored; accepted deliberately by the operator |
| R11 | File-based sessions pin the bot to one host | A future multi-worker deployment cannot share sessions | Accepted for v1 (single process); revisit with shared storage or a DB-backed store if scaling out |
| R12 | Legal/ToS of user-mediated downloads | Reputational | README states users may only download content they are entitled to; no moderation policy (spec §14) |
| R13 | Downloading resolved URLs ourselves (D25) | Sites that need a `Referer`, a specific `User-Agent` or per-site cookies will return 403 when we fetch directly; and a yt-dlp-backed item can resolve to a `webpage_url` or a DASH/HLS *manifest* rather than a fetchable media URL | Per-host header/cookie map; an explicit item-level rule — a manifest or non-media URL is either delegated to gallery-dl's own downloader or reported as a failure with a reason, never silently mangled. **Verify with a live `gallery-dl -j --simulate` run before writing the fetcher**, since gallery-dl falls back to `webpage_url` when the info dict has no direct `url` |

---

## 9. Milestones

| Milestone | Exit criterion | Phases |
|---|---|---|
| M0 DB provider | `spideybot db start` boots Postgres with Docker stopped; integration suite passes with and without Docker | P0-prereq |
| M1 Foundations | Fresh Postgres migrates; `/start` + `/help` answer; unit suite green | P0 |
| M2 Quota | AC-1, AC-10 pass | P1 |
| M3 Queue | Restart test proves no re-delivery; AC-4/5/6 pass | P2 |
| M4 Pipeline | AC-2, AC-3, AC-15 pass | P3 |
| M5 Status UX | Single-message lifecycle + coalescing verified; E7/E8 replays pass | P4 |
| M6 Sessions | AC-7 passes; restart reconnects from the session file | P5 |
| M7 Triggers | AC-8, AC-9 pass | P6 |
| M8 Extras | AC-11, AC-12, AC-13 pass | P7 |
| M9 Release | AC-14 + full AC-1…AC-15 in CI; live suite green; compose run green | P8 |

**Sequencing:** P0-prereq → P0 → P1 → P2 are strictly ordered. P3 and P4 interleave (a pipeline with no
status view has no observable behaviour). P5 needs P2+P3; P6 needs P5; P7 and P8 need P1+P3.

---

## 10. Immediate next step

**Batch 1 (P0-prereq + P0) is complete and committed.** The next action is Batch 2 (P1 + P2) per §12,
pending the operator's go. All three original prerequisites are closed:

1. `.gitignore` recreated (batch 0) — `git check-ignore -v` now matches both `.env` and `data/`.
2. The `spideybot db` CLI over `postgresql-binaries` brings up PostgreSQL with Docker stopped (batch 1).
3. The P0 scaffold in §4 exists, with 100 unit tests green and `ruff` clean.

R1's benchmark is captured in §0: 171.4 MiB/s with `tgcrypto` versus 0.59 MiB/s with `pyaes`, which is
why the accelerator is now a hard requirement rather than a preference. Still outstanding: a CI job
proving the `tgcrypto` source build stays green on each CPython bump.

---

## 11. Spec realignment (applied)

Every statement in [FUNCTIONAL_SPEC.md](./FUNCTIONAL_SPEC.md) that promised encryption has been
rewritten, because the system no longer encrypts anything (D9):

| Where | Now says |
|---|---|
| §1.3 privacy principle | cookies and sessions are "stored in service-account-only files, masked in the UI, and never logged in the clear" |
| FR-13, last line | "stored in a local session file and are revocable via `/logout`" |
| FR-14 rule | "Cookie status page states that the cookie is stored in a local file and never logged" |
| §7.6 login message | "⚠️ Your session is stored as a local file session." |
| §7.6 cookie page | "Your cookie is stored in a local file and never logged." |
| §8.4 cookie lifecycle | "No cookie → Set (from /setting) → Stored → View (masked) → Delete" |
| NFR-4 | "Cookies and sessions are stored in files restricted to the service account; masked in the UI; never logged in the clear." |
| §12 | points at §1 of this plan for the answers |
| §13 config table | the `session.encryption_key` row is removed |

No behavioural requirement other than storage-of-credentials was altered. AC-12's masking requirement
("View shows a masked value") still holds and is unaffected — masking is a UI behaviour, not encryption.

---

## 12. Execution cadence (agreed)

Work proceeds in **batches, one per phase group**, and **stops for review after each**. No batch starts
until the previous batch's gate is met or explicitly waived, and each verified batch is committed.

| Batch | Phases | Gate before moving on | Gate requires |
|---|---|---|---|
| 1 | P0-prereq + P0 | `spideybot db start` works with Docker stopped; migrations apply; `/start` + `/help` answer; `pytest tests/unit` green | network for pip + the 51 MB binaries download |
| 2 | P1 + P2 | AC-1, AC-4, AC-5, AC-6, AC-10, plus a kill-and-restart test proving no re-delivery | a local Postgres, nothing else |
| 3a | P3 core | AC-2, AC-3, AC-15 against a real link | a working link + network |
| 3b | P3 native adapters | one smoke test per adapter | credentials for the host under test |
| 4 | P4 + P5 + P6 | AC-7 via the live suite, AC-8, AC-9 | bot token, plus a test-DC or real account |
| 5 | P7 | AC-11, AC-12, AC-13 | live shortener hosts |
| 6 | P8 | AC-14, the full suite, and a compose run | Docker, or the bundled binaries |

Rules:

- **Stop and report after each batch** — state plainly what passed and what did not; never claim a gate
  passed when it was only partially exercised.
- **Commit each verified batch**, so any batch can be reviewed or reverted in isolation. `.gitignore` must
  exist first (it does).
- **Heavy optional dependencies are deferred.** `camoufox` and `playwright` pull ~100 MB+ of browser
  binaries that only `/bypass` needs, so they are installed at batch 5, keeping batches 1–4 fast.
- **Anything not verifiable locally** — a live TeraBox download, Cloudflare-gated shorteners, a real user's
  session — is implemented and tested against fakes, then handed over as a runnable checklist rather than
  reported as passing.
- **Batch 3b is the one parallel-friendly batch**: its eleven adapters are independent modules, each with a
  smoke test. Every other batch is a strict dependency chain and would serialise regardless.
