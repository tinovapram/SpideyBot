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
| D5 | Extraction | **gallery-dl is the general extractor**, and yt-dlp is invoked **as a library, for resolution only**, on native video sites (the YouTube family). Native adapters still run first; everything else goes to gallery-dl with `extractor.ytdl.enabled = true`. yt-dlp never transfers bytes for us — resolution hands back per-stream URLs and we fetch them (D25) | **Amended 2026-10 after probing; the original "our code never invokes yt-dlp" was already hollow.** gallery-dl imports yt-dlp lazily, so it is on the path either way, and its `ytdl` extractor hands back a `ytdl:https://…` **marker** in the URL slot (`extension: None`) rather than media — so for exactly those sites the single-surface claim bought nothing and cost clarity. Using `yt_dlp.YoutubeDL(...).extract_info(url, download=False)` directly is the most direct, best-maintained resolution route for YouTube, and the previous implementation already did this (`downloader/site/youtube.py::_fetch_ytdlp_fallback`, branch `V5`). Verified: the resolved pair's per-stream URLs are all present, so D25 still holds. Cross-check — handing D28 to yt-dlp as a format string (`bv*[vcodec^=avc1]+ba[ext=m4a]/bv*[vcodec^=avc1]+ba`) selects `299+140` at 1080p, the same pair `downloader/selection.py` chooses |
| D6 | Adapter scope | **Engine-first.** Engines cover the broad platform list; native adapters only where they fail: **TeraBox**, **Cyberdrop family**, the **YouTube family**, and the **ad-gated hosts** (Doodstream, StreamTape, MixDrop, StreamWish, Vidmonstr, Vidara, Luluvdoo, Bysejikuar, MEGA) | Each native adapter is a module + smoke test + a standing breakage surface (R4); breadth is free from the engines, so reliability is bought only where needed. **Chain order (user decision, 2026-10): native adapter → engine → `cyberdrop-dl` LAST**, only once every other method has failed — it is a heavyweight application with its own state rather than a peer engine, so it is a rescue path. TeraBox keeps its own path. This matches the previous implementation's `downloader/flow.py` (branch `V5`), which falls back native → gallery-dl → cyberdrop-dl, and its `downloader/site/*.py` is the reference for per-site behaviour. YouTube joins the native list because the *engine* cannot satisfy D25 there (it returns a `ytdl:` marker), which is exactly D6's stated test for buying a native adapter |
| D7 | **Sessions storage** | Kurigram native **File Storage**, **plaintext**: `data/sessions/<user_id>.session` | Operator decision. Simplest and self-healing; no key management. Accepted risk documented in §8/R10 |
| D8 | **Cookie storage** | Plaintext file: `data/cookies/<user_id>` | Mirrors D7 for consistency |
| D9 | **Encryption** | **None anywhere in the system.** No Fernet, no key management, no `user_secrets` table, and `SPIDEY_SESSION_ENCRYPT_KEY` is dropped | Follows from D7/D8 |
| D10 | Revocation | `/logout` calls `log_out()` **(server-side)** and *then* deletes the file | Deleting only the file leaves the auth key valid and the session listed in Active Sessions — the spec requires genuine revocation |
| D11 | Config layering | Committed `config/default.toml` (all §13 defaults as a reviewable `[tier.*]` table) → optional gitignored `config/local.toml` → `SPIDEY_*` env vars (highest) | Defaults become reviewable in a diff; TOML sources can be validated with `extra="forbid"` so a typo fails loudly instead of silently defaulting |
| D12 | Secrets | Bootstrap secrets only (bot token, API hash, DB password, TeraBox/Reddit creds) via a `SecretsProvider` seam; env today, keyring/Docker secrets later | Keeps non-secret config separable and makes an OS secret store a drop-in |
| D13 | Structure | **Shallow layers**: `main.py`, `app/`, `handlers/`, `downloader/`, `telegram/`; nothing deeper than two levels, no `src/` nesting | Operator decision. The package is `downloader/`, not `download/`, so the code that *downloads* is never confused with `data/downloads/`, where the bytes land — and it matches the previous implementation's naming |
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
| D28 | Media selection | Compatibility-first: prefer H.264 + AAC in MP4, using the best available stream (AV1/Opus) only when nothing compatible exists. **Settled 2026-10 as A+C: compatibility-first by default, with the choice exposed as `[download] video_preference = "compatible" \| "quality"`.** Chosen while the user was unavailable, on the grounds that it satisfies the spec's "use AV1/Opus only when nothing compatible exists", keeps the tier table about limits rather than quality, and is reversible by changing one config value | A file that will not play is not a successful download: AV1 in Telegram is client-dependent, while H.264/AAC in MP4 is universally playable and merges by stream copy (no transcode). Measured on the probed video: compat `299+140` = **1080p, ~255 MB**; yt-dlp's own default `401+251` = **2160p, ~689 MB**. The size is the stronger argument — the daily bandwidth cap, not the download count, is what really limits a free user (~4 downloads/day at 255 MB against ~1.5 at 689 MB), and 689 MB exceeds a free user's 500 MB link limit outright. A stream we cannot play is still delivered, as a document with the reason in the caption, rather than failed |
| D29 | Upgrade button | `[ux] upgrade_url` is operator-supplied and empty by default; while it is unset the button is omitted rather than pointing somewhere invented | AC-1 requires the button but no destination appears anywhere in the spec. Validated at startup to be a link Telegram accepts (http/https/tg) so a typo fails immediately instead of as an API error on the first `/start`. A button-free welcome still satisfies the rest of AC-1 |
| D30 | Resolver output | **A file to deliver is one *or more* transfers**, and sizes are all-or-nothing: a total is reported only when every stream reported one | Every YouTube format is a merge (measured: 0 of 53 carry audio), so "one file = one URL" cannot express what a job actually does — and the fetcher needs per-stream URLs to report byte progress, while the size gate needs the size of the *result* rather than of whichever stream was counted first. All-or-nothing exists because a partial sum looks precise: `None` for an unknown size must never read as `0`, or the gate waves through a download it cannot measure (E3/AC-3), while a reported `0` is a real measurement and E6 has a specific message for it |
| D31 | Size gate on an unknown size | An unreported size **passes, flagged for enforcement during transfer** — the fetcher aborts if the running total passes the cap | Refusing would break links that would have worked, since many extractors publish no size; allowing it silently would let an oversized share through and spend the user's daily quota discovering that. Flagging keeps one rule (the limit holds) and enforces it where it can be. `limit_mb = 0` means unlimited, so there is no cap to enforce |

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
                 │  handlers/  │  downloader/flow ──▶ engines + sites + bypass             │
                 │  magic_word │             │                                             │
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
  downloader/              # the code that downloads; the bytes land in data/downloads/
    engines.py  flow.py  probe.py  registry.py  selection.py
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
- [x] Fresh Python 3.14 venv via `uv`; `uv pip install -r requirements-dev.txt` (runtime dependencies plus
      pytest and ruff — split so the image installs neither).
      `cryptography` is absent because D9 encrypts nothing, and `camoufox` is deferred to batch 5 per
      §12. `tgcrypto` compiles from the sdist (verified) and is now a **required** capability (D27)
- [x] `config/default.toml`, `config/local.toml.example`, `.env.example`; `git check-ignore` confirms both
      `.env` and `data/`
- [x] `app/config.py` — 4-layer chain, `extra="forbid"` on TOML, `SecretStr`, `SecretsProvider`
- [x] `app/logging.py` (secret redaction), `app/models.py`, Alembic baseline (nine tables, applied).
      Alembic autogenerate emits single-quoted, 79-column code, so **any regenerated or new migration
      needs `ruff format migrations/` afterwards** — otherwise `ruff format --check` fails and the next
      `ruff format .` re-churns the file
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
- [x] Onboarding state machine: registration, silent referral capture, first-vs-repeat `/start` (FR-1)
- [x] Tier resolution with expiry revert; admin precedence (FR-2)
- [x] Quota check / reserve / commit / release; daily boundary (FR-3.2, FR-15, §8.5). The reservation
      is one `INSERT … ON CONFLICT DO UPDATE … WHERE` statement, so a check and an increment cannot be
      interleaved; a ten-way concurrent test proves the allowance cannot be oversold
- [x] `/quota` with `∞` rendering (FR-15, AC-10). Reads the same rows the gates read rather than
      recomputing them, so the display cannot drift from the enforcement. `0` deliberately means
      different things for a count (`no allowance`, matching `reserve_download`) and a size
      (`unlimited`, spec 13), and both are exercised
- [x] Submission rate limiter (D22): per-user token bucket, injected clock. A fixed window would let
      a user spend the whole allowance at 11:59:59 and the next at 12:00:00, so it refills
      continuously instead. Buckets are swept out once they refill, and `check` sweeps idle ones
      itself past a threshold, so the dictionary cannot grow with every user ever seen

**Done when:** AC-1 and AC-10 pass. — **Both met**; AC-1 and AC-10 are covered in `tests/acceptance`.

### P2 — Queue, jobs, cancellation
- [x] `app/queue.py`: priority + submission order, lease claim/renew/requeue (FR-10).
      Verified against Postgres, because these are database properties: four concurrent claimers
      never share a job (`FOR UPDATE SKIP LOCKED`), the slot count travels inside the claiming
      statement so it cannot oversell a tier, a stale claim cannot renew its way back in, and a
      job that already finished cannot be terminated a second time (E26)
- [x] Workers: per-tier slots, release on terminal state. The pool holds `max_concurrent_downloads`
      tasks and the *claim* applies the per-tier limit, so concurrency policy lives in one place;
      a lease-renewal task keeps a long download from being handed to a second worker, and a run
      that lost its lease discards its result instead of overwriting the new owner's. Cancellation
      reaches a running job through the keeper, which is how FR-11.6 is achievable at all
- [x] `/cancel` and `/status` with ownership/unknown-id refusals (FR-11, FR-12). The command only
      *asks*: whether a job stops at once (it was still waiting) or a request is recorded for the
      worker to act on (it was mid-transfer) is decided in `app/queue` with the row in hand, so the
      refusals cannot be raced. A requested cancellation already reads as `❌ Cancelled`, since
      showing `🔄 Running` while the worker unwinds would look like `/cancel` did nothing
- [x] Restart recovery gated on `files_sent`; delivered jobs never re-run (§8.2, E26). The
      `job_items` ledger itself arrives with the pipeline (P3), but the recovery half is verified:
      a job abandoned by a killed process is resumed and delivered exactly once, a delivered job
      is never picked up again by the new process, and a cancellation requested before the crash
      is honoured rather than revived
- [x] Scheduler: lease sweep, retention, orphan sweep (D18, D21). **Not yet started by `run`**: a
      worker with no job handler would claim jobs and fail them, so the pool and the scheduler are
      wired up with the pipeline in P3. The claim/lease/recovery path itself is verified here

      Deliberately **no daily reset job**: usage is one row per user per day, so a new day *is* an
      empty allowance. A scheduled reset would be a second implementation of the same rule, and the
      wrong one whenever the process was down at midnight; `tests/integration/test_scheduler.py`
      tests the boundary instead. The injected clock (D18) is what makes "retention is due"
      testable without waiting an hour

**Done when:** AC-4, AC-5, AC-6 pass, including kill-and-restart with no re-delivery.

### P3 — Download pipeline & delivery (engine-first, D6)

**Probed 2026-10 with gallery-dl 1.32.15 / yt-dlp 2026.08.19 — the engine output is not what the
plan assumed, so these are requirements rather than notes (details in R13):**

- `gallery-dl -j --simulate <url>` prints **one pretty-printed JSON array**, not one object per
  line. The whole stdout is a single `json.loads`, and it is ~47 KB for a one-file page.
- Every record is **typed and positional**: `[2, {metadata}]` is a directory/header,
  `[3, "<url>", {metadata}]` carries the file URL in slot 1, and `[-1, {error, message}]` is a
  failure. There is no `url` key to read — the URL is not a dict field.
- **`gallery-dl` exits 0 even when it resolved nothing**: a 404 came back as exit 0 plus a `[-1]`
  record. Exit code alone is not a success signal, so the fetcher must scan for negative codes.
- **`gallery-dl` does not handle YouTube by default** ("Unsupported URL", exit 64). The `ytdl`
  extractor must be enabled with `-o extractor.ytdl.enabled=true`, and then the `[3]` record's URL
  slot is a **`ytdl:` marker with `extension: None`** — the real, time-limited media URLs live in
  `metadata["formats"]` (53 entries here) and `metadata["requested_formats"]` (2). Because those
  URLs carry `expire=…`, resolution and transfer must stay in the same job: resolving a link and
  fetching it an hour later would use a dead URL.
- `yt-dlp -j` prints **one JSON object** (single document, 79 keys here) — a different shape from
  gallery-dl, so the two engines need separate parsers rather than one shared "JSON lines" reader.
- `yt-dlp`'s top-level dict can have **no `url` at all**: this video resolved to a merge
  (`protocol: https+https`, `requested_formats` of length 2), with the URLs only inside
  `requested_formats`. So D25's "fetch the resolved URL ourselves" needs an explicit rule for the
  merge case — either two fetches plus an ffmpeg merge, or a delegated download — and must never
  read `webpage_url` as if it were media (R13's warning, now confirmed by observation).
- **There are no single-file formats left on YouTube** (0 of 53 here, 0 of 24 on a second video),
  so *every* YouTube job is a two-stream merge and ffmpeg is on the critical path for all of them,
  not only for high-quality picks (D27).
- Resolved URLs may carry tracking parameters (`?utm_source=…`); they are kept verbatim, since
  stripping query strings would break signed URLs.
- Per-site behaviour (which hosts need a native adapter, what each one needs to resolve) is
  grounded in the previous implementation: `downloader/site/*.py` and `downloader/flow.py` on
  branch `V5`, which is also where the native → gallery-dl → cyberdrop-dl ordering comes from.

- [x] `downloader/selection.py`: which streams to fetch, and whether the result will play (D28).
      Written against the *real* format lists, so the traps it closes were observed rather than
      imagined: `formats` contains storyboard entries (`protocol: mhtml`) a naive largest-pick can
      select, and a stream with no URL is not a candidate at all — which is what stops a
      `ytdl:`-marked item being "fetched" as though its marker were media (R13)
- [ ] Native YouTube adapter: resolve with `yt_dlp.YoutubeDL(...).extract_info(url, download=False)`,
      choose with `downloader/selection.py`, fetch and merge ourselves. This is a *native* adapter under
      D6's rule because the engine fails D25 here, not because YouTube needs special handling —
      gallery-dl's `ytdl` route yields a marker rather than media. Carried over from `V5`'s
      `_fetch_ytdlp_fallback`, which proved the library call. **Not** carried over: V5's primary path,
      a POST to `api.vidssave.com` with a hardcoded token (`auth: 20250901majwlqo`) and a
      `Referer` from someone else's site — an expiring third-party dependency that would sit in the
      middle of every YouTube download, for a resolve we can do ourselves
- [x] `downloader/contracts.py` + `downloader/probe.py`: the resolver contract and the engines' output
      (D30). Two engines, two shapes, one contract: gallery-dl's typed records (`[3, url, meta]`,
      `[-1, error]`) and yt-dlp's single info dict are both translated into files and transfers.
      Guards that exist because the probes contradicted the plan: the `-o extractor.ytdl.enabled=true`
      flag is asserted present (without it YouTube is "Unsupported URL", exit 64), a `ytdl:` marker is
      **never** fetched as media but re-resolved from the metadata's formats, and no code path reads
      the exit code as a success signal. Both guards were mutation-checked — disabling the marker rule
      produces a file whose only transfer is the marker itself, which is R13's trap exactly
- [ ] `downloader/registry.py` + `downloader/flow.py`: host → engine, native overrides, and the
      chain native → gallery-dl → `cyberdrop-dl` last
- [ ] Engines: yt-dlp, gallery-dl, cyberdrop-dl, generic HTTP (`curl_cffi`)
- [ ] Engines: yt-dlp, gallery-dl, cyberdrop-dl, generic HTTP (`curl_cffi`)
- [ ] Native adapters: TeraBox (account pool + per-user cookie), Cyberdrop family, Doodstream,
      StreamTape, MixDrop, StreamWish, Vidmonstr, Vidara, Luluvdoo, Bysejikuar, MEGA
- [ ] Fallback chain with a visible phase change (E2, §3); pre-download size gate (E3, AC-3)
- [x] Pre-transfer size gate (`downloader/gate.py`) and the failure block (`app/catalog.py`), both
      pure so they can be verified before any engine call exists. AC-3's refusal is decided on what
      resolution reported, never after transfer; the failure list is capped at 10 bullets with a
      remainder (FR-9.3/9.4) and a filename with brackets is escaped so it cannot break the link
      markup it appears in. An unreported size passes but is flagged for enforcement during transfer
      (D31) — checked because `0` and `None` had already been conflated once in D30's code
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
      Measured layers: ffmpeg 457 MB, pip install 331 MB, Python base 88 MB. Two cheap wins are applied —
      the 77 MB wheels layer is gone (mounted, not copied) and dev tooling no longer ships
      (`requirements-dev.txt`). Still open: a **static ffmpeg build would save a further ~380 MB**, plus
      a compose service and the README's provider table

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
| FR-4 `/dt` | P5 | `downloader/telegram_source` + `handlers/tg_download.py` | AC-7 |
| FR-5 magic word | P6 | `handlers/magic_word.py` | AC-8 |
| FR-6 TimerMedia | P6 | `handlers/timer_media.py` | AC-9 |
| FR-7 progress/status | P4 | `telegram/status.py` | AC-2, E7, E8 |
| FR-8 delivery format | P3 | `telegram/delivery.py` | AC-2 |
| FR-9 failure reporting | P3 | `downloader/flow.py` + catalog | AC-15, E27 |
| FR-10 queue & concurrency | P2 | `app/queue.py` | AC-4 |
| FR-11 cancellation | P2 | `handlers/cancel.py` | AC-5 |
| FR-12 `/status` | P2 | `handlers/job_status.py` | AC-6 |
| FR-13 accounts/sessions | P5 | `handlers/login.py`, `app/sessions.py` | AC-7 |
| FR-14 settings menu | P7 | `handlers/setting.py` | AC-12 |
| FR-15 `/quota` | P1 | `handlers/quota_info.py` | AC-10 |
| FR-16 `/sites` | P7 | `handlers/sites.py` | — |
| FR-17 referrals | P7 | `app/referral.py`, `handlers/referral.py` | AC-11 |
| FR-18 bypass | P7 | `downloader/bypass/` | AC-13 |
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
| R10 | **Plaintext session/cookie files** (D7–D9) | A copied file is a logged-in device, no password needed | Credential directories are created **0700** (verified in the container: `0o700` on sessions, cookies and the bot's own session, with `insecure_secret_dirs()` returning `[]`), and a startup warning still fires if one is ever loosened; one file per user; never logged or exported; gitignored; accepted deliberately by the operator |
| R11 | File-based sessions pin the bot to one host | A future multi-worker deployment cannot share sessions | Accepted for v1 (single process); revisit with shared storage or a DB-backed store if scaling out |
| R12 | Legal/ToS of user-mediated downloads | Reputational | README states users may only download content they are entitled to; no moderation policy (spec §14) |
| R13 | Downloading resolved URLs ourselves (D25) | Sites that need a `Referer`, a specific `User-Agent` or per-site cookies will return 403 when we fetch directly; and a yt-dlp-backed item can resolve to a `webpage_url` or a DASH/HLS *manifest* rather than a fetchable media URL | **Probed 2026-10 with gallery-dl 1.32.15 and yt-dlp 2026.08.19; the plan's assumption was wrong in a way that would have produced a broken parser.** `gallery-dl -j` emits one pretty-printed JSON *array* whose records are typed and positional — `[3, "<url>", {meta}]` for a file, `[2, {meta}]` for a directory, `[-1, {error, message}]` for a failure — so the URL is **not** a dict key, and it is **exit 0 on failure**, which means the exit code cannot be the success signal. For yt-dlp-backed sites the record's URL slot is a **`ytdl:` marker** carrying no media at all (`extension: None`), so the fetchable URLs must be read from `metadata["formats"]` / `metadata["requested_formats"]` — and those are time-limited, so a resolved job must be fetched promptly rather than queued against a stale URL. `yt-dlp -j` emits **one JSON object** (a different shape, needing its own parser) and its top-level dict can have **no `url`**: the probed video resolved to a merge (`protocol: https+https`, `requested_formats` length 2) with the URLs only inside `requested_formats`. Neither engine offers a single-file YouTube format any more (0 of 53), so the merge path is the normal path. Per-host header/cookie map still required |
| R14 | **`.env` quoting was read differently by `python-dotenv` and Docker's `--env-file`** | A value quoted the way `.env.example` recommends loaded correctly on the host but arrived **still quoted** in a container, so startup failed validation — a failure that never reproduces locally | **Fixed.** `load_env_file` normalises `SPIDEY_*` values in the environment (`strip_env_quotes`), so one `.env` behaves identically in both places. Verified by re-running the container with the real `.env`, which previously failed with `invalid literal for int()`. `.env.example` now documents only what `--env-file` also supports, marking `${VAR}` expansion and inline comments as host-only |

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
