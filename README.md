# SpideyBot V5 — Telegram Media Downloader Bot

Download media from **TeraBox**, **Vidmonstr**, **Twitter/X**, **Instagram**, **TikTok**,
**YouTube**, **Reddit**, **Bluesky**, **Pinterest**, **Spotify**, and 28+
platforms — all from a single Telegram bot. Send a link, get the files in chat.

## Features

- **28+ platforms** — TeraBox, YouTube, Twitter/X, Instagram, TikTok, Reddit, and more
- **Telegram-native downloads** — `/dt` pulls files directly from Telegram messages via your own session
- **Tiered access** — Free, Pro, and Premium tiers with configurable limits
- **Persistent queue** — PostgreSQL-backed job queue with priority scheduling
- **Concurrency control** — per-tier slot limits (1/2/3); extra links wait in the queue
- **Per-file progress** — live counts such as `Uploading (file 7/25)` while a job runs
- **Named failure reports** — any file that fails to download or upload is listed in the
  final message with its filename and the reason
- **Referral system** — Earn bonus downloads by inviting friends
- **Per-user cookies** — Encrypted per-user TeraBox session cookies
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
├── handler/           # Telegram handlers (user, admin, login, outgoing, setting)
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

```bash
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate
pip install -r requirements.txt

# Fetch Camoufox browser (needed for /bypass and some downloaders)
camoufox fetch

# Start PostgreSQL (Docker or local)
docker compose up -d postgres

# Apply database migrations
alembic upgrade head

# Run the bot
python main.py
```

## Configuration

All settings use the `SPIDEY_` env prefix. Copy `.env.example` to `.env` and fill in:

| Variable | Required | Description |
|----------|----------|-------------|
| `SPIDEY_TG_API_ID` | Yes | Telegram API ID from [my.telegram.org](https://my.telegram.org) |
| `SPIDEY_TG_API_HASH` | Yes | Telegram API hash |
| `SPIDEY_TG_BOT_TOKEN` | Yes | Bot token from [@BotFather](https://t.me/BotFather) |
| `SPIDEY_DATABASE_URL` | Yes | PostgreSQL connection string |
| `SPIDEY_SESSION_ENCRYPT_KEY` | Yes | Fernet key for cookie encryption |
| `SPIDEY_ADMIN_IDS` | Yes | Comma-separated Telegram user IDs |
| `SPIDEY_TERABOX_COOKIES` | No | TeraBox cookie(s) — single or `\|`-delimited for multi-account |

## User tiers

| Tier | Files/day | Max link size | Daily bandwidth | Concurrency |
|------|-----------|---------------|-----------------|-------------|
| Free | 10 | 500 MB | 1 GB | 1 |
| Pro | 50 | 10 GB | 25 GB | 2 |
| Premium | 200 | Unlimited | 100 GB | 3 |

A download that would exceed the concurrency limit waits in the queue and starts as
soon as a slot frees up. Admins run with the Premium slot count but are never
rejected at submission — their extra links always queue.

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
| `/logout` | Disconnect your active session |
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
pytest               # 148 tests
pytest -v            # verbose
pytest tests/test_handler.py   # handler tests only
```

## License

MIT
