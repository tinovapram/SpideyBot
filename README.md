# SpideyBot — Telegram Media Downloader Bot

Download media from **TeraBox**, **Twitter/X**, **Instagram**, **TikTok**,
**YouTube**, **Reddit**, **Bluesky**, **Pinterest**, **Spotify**, and 28+
platforms — all from a single Telegram bot. Send a link, get the files in chat.

## Features

- **28+ platforms** — TeraBox, YouTube, Twitter/X, Instagram, TikTok, Reddit, and more
- **Telegram-native downloads** — `/dt` pulls files directly from Telegram messages via your own session
- **Predictable delivery** — photos and videos arrive as albums (max 10 per group); documents, audio and archives are sent as separate messages
- **Tiered access** — Free, Pro, and Premium tiers with configurable limits
- **In-memory queue** — process-local priority scheduling; queued jobs are cleared on restart
- **Rate limiting** — per-user command limits, plus paced media preparation and sends so large requests stay flood-safe
- **Referral system** — Earn bonus downloads by inviting friends
- **Per-user cookies** — Encrypted per-user TeraBox session cookies; TeraBox downloads require your own `ndus` (admins exempt)
- **User sessions** — On-demand Telegram account sessions with automatic lifecycle
- **Admin tools** — Stats, premium management, and user oversight
- **Camoufox browser** — Anti-detection browser for sites that block headless clients

## Architecture

```
SpideyBot/
├── main.py            # entry point
├── core/              # config, database, queue, sessions, tiers, referral
├── downloader/        # downloaders (base, registry, gallery-dl, TeraBox, per-site)
├── utils/             # paths, files, formatting, progress, Telegram helpers
├── handler/           # Telegram handlers (handler, admin, login, setting, bypass)
├── migrations/        # Alembic database migrations
├── downloads/         # runtime download staging (gitignored)
├── config/            # gallery-dl / yt-dlp config templates
└── tests/             # pytest suite
```

## Quick start (Docker)

```bash
cp .env.example .env   # fill in your credentials
docker compose up --build -d
docker compose logs -f
```

The container runs PostgreSQL + SpideyBot. Camoufox (anti-detection browser)
is baked into the image during build. Bind-mounted volumes persist data,
user sessions, and gallery-dl config across restarts.

## Quick start (local)

[uv](https://docs.astral.sh/uv/) is used by CI and the Docker build; `uv.lock` pins every
dependency.

```bash
uv sync --frozen --extra test     # creates .venv from uv.lock

# Fetch Camoufox browser (needed for /bypass and some downloaders)
uv run camoufox fetch

# Start PostgreSQL (Docker or local)
docker compose up -d postgres

# Apply database migrations
uv run alembic upgrade head

# Run the bot
uv run python main.py
```

Prefer pip? `python -m venv .venv && source .venv/bin/activate && pip install ".[test]"`
(or `.venv\Scripts\activate` on Windows) works too, but will not honour `uv.lock`.

## Configuration

All settings use the `SPIDEY_` env prefix. Copy `.env.example` to `.env` and fill in:

| Variable | Required | Description |
|----------|----------|-------------|
| `SPIDEY_TG_API_ID` | Yes | Telegram API ID from [my.telegram.org](https://my.telegram.org) |
| `SPIDEY_TG_API_HASH` | Yes | Telegram API hash |
| `SPIDEY_TG_BOT_TOKEN` | Yes | Bot token from [@BotFather](https://t.me/BotFather) |
| `SPIDEY_DATABASE_URL` | Yes | PostgreSQL connection string |
| `SPIDEY_SESSION_ENCRYPT_KEY` | Yes | Fernet key for TeraBox cookies and legacy session files |
| `SPIDEY_ADMIN_IDS` | Yes | Comma-separated Telegram user IDs |
| `SPIDEY_TERABOX_COOKIES` | No | Bot-owned TeraBox account(s) — single or `\|`-delimited. Used by admins and the shared pool; **users still need their own `ndus` cookie** for TeraBox downloads |
| `SPIDEY_MEDIA_CONCURRENCY` | No | Media prepared in parallel on the shared bot client (default 5; `0` follows `SPIDEY_SESSIONLESS_WORKER_SLOTS`). Logged-in users prepare on their own session, bounded by their tier concurrency |
| `SPIDEY_MEDIA_PER_MINUTE` / `SPIDEY_MEDIA_BURST` | No | Media prepared per minute per client (default 60), and how many may start at once (default 5) |
| `SPIDEY_SEND_CHAT_PER_MINUTE` / `SPIDEY_SEND_BURST` | No | Message RPCs per chat (defaults 20 / 5); an album of 10 media costs one |
| `SPIDEY_SESSIONLESS_WORKER_SLOTS` | No | Workers reserved for users without a session (default 5 of `SPIDEY_MAX_CONCURRENT_DOWNLOADS`); `0` disables the split |
| `SPIDEY_SESSION_IDLE_SECONDS` / `_PRO_` / `_PREMIUM_` | No | Idle timeout before a live client is disconnected, per tier (defaults 30 min / 12 h / 24 h). The session file is kept, so `/start` reconnects |

Unknown `SPIDEY_*` keys are logged at startup instead of being silently ignored, so a typo in
`.env` is visible in the logs.

### Security notes

- `user_sessions/<user id>.session` files are **plaintext** Telethon SQLite databases. They are
  created with owner-only permissions, but they are not encrypted at rest — protect the volume
  (or the host) accordingly.
- `SPIDEY_SESSION_ENCRYPT_KEY` encrypts per-user TeraBox cookies and decrypts legacy string
  sessions; it does not encrypt the file sessions above.
- `/dl` links are rejected when they resolve to private, loopback or link-local addresses.
  Redirects followed inside yt-dlp / gallery-dl are outside that check, so keep the container's
  egress restricted if the bot is exposed to untrusted users.

## User tiers

| Tier | Downloads/day | Per-link size | Daily bandwidth | Concurrency | Queue priority |
|------|---------------|---------------|-----------------|-------------|----------------|
| Free | 10 | 500 MB | 1 GB | 1 | 2.0 |
| Pro | 50 | 10 GB | 25 GB | 3 | 1.0 |
| Premium | 200 | Unlimited | 100 GB | 5 | 0.5 |

All tiers can use every supported site. Every value is overridable through the
`SPIDEY_TIER_*` environment variables (`..._CONCURRENT_MODE` chooses whether a request is
rejected or queued when the concurrency slots are full). Admins bypass the daily limits and
queue ahead of everyone (priority 0.2).

## Commands

| Command | Description |
|---------|-------------|
| `/start` | Sign up, refresh your status, or auto-start a saved session |
| `/help` | Show help guide |
| `/dl <URL>` | Download from any supported platform |
| `/dt <t.me link>` | Download directly from a Telegram message (requires `/start` or `/login`) |
| `/cancel <ID>` | Cancel a queued download |
| `/status` | View your active downloads |
| `/account` | Account & session status |
| `/quota` | View your usage & limits |
| `/sites` | List supported platforms |
| `/referral` | Get your invite link & stats |
| `/login` | Start a Telegram user session (interactive 2FA flow) |
| `/stop` | Disconnect your session but keep the saved login (`/start` reconnects) |
| `/logout` | Revoke your session and delete the stored login |
| `/bypass <URL>` | Resolve link shorteners (ouo, adfly, gplinks, etc.) to final destination |

### Admin commands

| Command | Description |
|---------|-------------|
| `/stats` | View live download statistics |
| `/addpremium` | Grant Pro tier to a user |
| `/removepremium` | Revoke Pro tier from a user |
| `/checkpremium` | Check a user's premium status |

## User settings (/setting)

The `/setting` command opens an interactive button menu for per-user preferences:

- **TeraBox Cookie** — Set, view, or delete your TeraBox session cookie
- **TimerMedia** — Toggle auto-save of private photos/videos (default: ON)
- **Magic Word** — Change the word that triggers media download when replying (default: `WOW`, case insensitive)

1. Press **/setting** → tap **TeraBox Cookie**
2. **Set** → sends your cookie with `/setting tera set ndus=...`
3. **View** → shows your masked saved cookie
4. **Delete** → confirms and removes your cookie

Cookies are encrypted with Fernet and stored in the database.
Free-tier cookies are shared across the bot's account pool; Pro/Premium cookies stay private.

## Testing

```bash
uv run pytest                        # full suite
uv run pytest -v                     # verbose
uv run pytest tests/test_queue.py    # one module

# Database-backed tests are skipped unless a database is provided:
SPIDEY_TEST_DATABASE_URL=postgresql+asyncpg://spidey:spidey@localhost:5432/spideybot \
  uv run pytest
```

## License

MIT
