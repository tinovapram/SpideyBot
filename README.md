# SpideyBot V4 — Telegram Media Downloader Bot

Download media from **TeraBox**, **Twitter/X**, **Instagram**, **TikTok**,
**YouTube**, **Reddit**, **Bluesky**, **Pinterest**, **Spotify**, and 20+ more
platforms — all from a single Telegram bot. Send a link, get the files in chat.

## Features

- **30+ platforms** — TeraBox, YouTube, Twitter/X, Instagram, TikTok, Reddit, and more
- **Tiered access** — Free, Pro, and Premium tiers with configurable limits
- **Persistent queue** — PostgreSQL-backed job queue with priority scheduling
- **Rate limiting** — Per-user rate limits to prevent abuse
- **Referral system** — Earn bonus downloads by inviting friends
- **Per-user cookies** — Encrypted per-user TeraBox session cookies
- **User sessions** — On-demand Telegram account sessions with automatic lifecycle
- **Admin tools** — Stats, premium management, and user oversight

## Architecture

```
SpideyBot/
├── main.py            # entry point
├── core/              # config, database, queue, sessions, tiers, referral
├── downloader/        # downloaders (base, registry, gallery-dl, TeraBox, per-site)
├── utils/             # paths, files, formatting, progress, Telegram helpers
├── handler/           # Telegram handlers (user, admin, login, outgoing, cookie)
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

## Quick start (local)

```bash
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate
pip install -r requirements.txt

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

| Tier | Files/day | Size limit | Daily bandwidth | Monthly | Concurrency | Sites |
|------|-----------|------------|-----------------|---------|-------------|-------|
| Free | 10 | 100 MB | 1 GB | 20 GB | 1 | Social |
| Pro | 50 | 2 GB | 25 GB | 500 GB | 3 | Social + Video |
| Premium | 200 | 10 GB | 100 GB | 2 TB | 5 | All |

Admins bypass all limits.

## Commands

| Command | Description |
|---------|-------------|
| `/start` | Sign up or refresh your status |
| `/help` | Show help guide |
| `/dl <URL>` | Download from any supported platform |
| `/dt <URL>` | Download and auto-split into 1 GB chunks |
| `/cancel <ID>` | Cancel a queued download |
| `/status` | View your active downloads |
| `/account` | Account & session status |
| `/quota` | View your usage & limits |
| `/sites` | List supported platforms |
| `/referral` | Get your invite link & stats |

### Admin commands

| Command | Description |
|---------|-------------|
| `/stats` | View live download statistics |
| `/addpremium` | Grant Pro tier to a user |
| `/removepremium` | Revoke Pro tier from a user |
| `/checkpremium` | Check a user's premium status |

## Per-user TeraBox cookies

Users can set their own TeraBox cookies via the `/cookie` command:

```
/cookie set ndus=YOUR_NDUS_VALUE
/cookie status
/cookie delete
```

Cookies are encrypted with Fernet and stored in the database.

## Testing

```bash
pytest
```

## License

MIT
