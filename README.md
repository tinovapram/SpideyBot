# SpideyBot — Telegram Media Downloader Bot

Download media from **TeraBox**, **Twitter/X**, **Instagram**, **TikTok**,
**YouTube**, **Reddit**, **Bluesky**, **Pinterest**, **Spotify**, and 20+ more
platforms — all from a single Telegram bot. Send a link, get the files in chat.

## Project structure

```
SpideyBot/
├── main.py            # entry point
├── core/              # config, database, queue, sessions, logging, bot lifecycle
├── downloader/        # downloaders (base, registry, gallery-dl, TeraBox, per-site)
├── utils/             # paths, files, formatting, progress, Telegram helpers
├── handler/           # Telegram handlers (user, admin, login, outgoing)
├── downloads/         # runtime download staging (gitignored)
├── config/            # gallery-dl / yt-dlp config templates
└── tests/             # pytest suite
```

## Highlights

- **30+ platforms** — TeraBox, YouTube, Twitter/X, Instagram, TikTok, Reddit, and more
- **Smart queue** — priority scheduling with anti-starvation aging and per-user limits
- **User account mode** — log in with your own Telegram account for private content
- **File-based sessions** — each user's session is a Telethon SQLite `.session` file
  under `./user_sessions`, with automatic migration from the legacy encrypted
  StringSession format.
- **Flood-wait aware** — Telegram rate limits are waited out automatically

## Setup

```bash
pip install -r requirements.txt
cp .env.example .env   # fill in TG_API_ID, TG_API_HASH, TG_BOT_TOKEN, TERABOX_COOKIE
python main.py
```

### Docker

```bash
docker compose up --build -d
docker compose logs -f
```

## User sessions

Sessions are stored as Telethon **file sessions** (`user_sessions/<user_id>.session`).
On first use after upgrading, any legacy encrypted StringSession stored in
`user_sessions/<user_id>.json` is decrypted and converted to a `.session` file
automatically (`SESSION_ENCRYPT_KEY` is required for that one-time migration).

## Commands

| Command | Description |
|:---|:---|
| `/dl <link>` | Download from any supported platform |
| `/cancel` | List or cancel your downloads |
| `/login` / `/logout` | Connect / revoke your Telegram account |
| `/ping` | Test your session |
| `/help` | Full command list |

Admin: `/addpremium`, `/removepremium`, `/checkpremium`.

## Tests

```bash
pytest
```
