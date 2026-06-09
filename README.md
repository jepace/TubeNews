# TubeNews

Turn any YouTube channel into a personalised AI-written news feed.

TubeNews monitors YouTube channels, transcribes new videos via [Supadata](https://supadata.ai), and uses Google Gemini AI to write AP-style news stories from the content. Each user gets a personalised feed filtered to the topics they care about, served through a web UI with subscriptions, an inbox, and shareable feed pages.

## What It Does

1. Subscribes to YouTube push notifications (WebSub) for configured channels
2. Fetches full transcripts (with timestamps) via the Supadata API
3. Sends transcripts to Gemini AI with a journalistic prompt focused on your configured topics
4. Saves AI-generated news stories as Markdown files
5. Serves stories through a web UI; also publishes per-user and per-channel RSS feeds

## Quick Start

```bash
# Install dependencies (global install — no venv needed)
pip install -r requirements.txt

# Configure
cp config.json.sample config.json
# Edit config.json: add gemini_api_key, supadata_api_key, base_url

# Start the daemon (subscribes to YouTube push, processes new videos continuously)
python3 TubeNews.py

# Start the web UI (in a separate terminal)
./serve.sh
# Open http://your-server:8000
```

First time on a channel with existing videos? Run `python3 helpers/catchup.py` before starting the daemon to avoid reprocessing the entire backlog.

See `SERVING.md` for production deployment (gunicorn, HTTPS, reverse proxy).

## Requirements

- Python 3.10+
- [Supadata API key](https://supadata.ai) — for transcript extraction
- [Google Gemini API key](https://aistudio.google.com) — for AI story generation
- A public HTTPS URL (for WebSub push notifications from YouTube)

## Configuration

```bash
cp config.json.sample config.json
```

Key fields in `config.json`:

```json
{
  "gemini_api_key": "YOUR_GEMINI_KEY",
  "gemini_model": "gemini-2.5-flash",
  "supadata_api_key": "YOUR_SUPADATA_KEY",
  "base_url": "https://yourdomain.com",
  "websub_callback_url": "https://yourdomain.com/youtube/push",
  "websub_secret": "generate-with-python3-secrets-token-hex-32",
  "tubenews_key": "generate-with-python3-secrets-token-hex-32"
}
```

Channel configuration lives in `state/channels.json` and is managed via the web UI admin panel. See `config.json.sample` for all available options including ntfy notifications, email digests (via Resend), and daemon tuning.

## Running

```bash
# Daemon mode (default): subscribes to YouTube WebSub, processes pushes continuously
python3 TubeNews.py

# Single-run mode: process all channels once and exit (good for cron)
python3 TubeNews.py --single-run

# Add --debug for verbose logging
python3 TubeNews.py --debug

# Web UI (always use serve.sh, never python3 web/app.py)
./serve.sh
```

Most `config.json` settings are hot-reloaded each processor cycle — no restart needed for API key or tuning changes.

## Storage

```
content/
├── <channel_slug>/
│   ├── <video_id>/
│   │   ├── transcript.txt    # Supadata transcript
│   │   ├── metadata.json     # Processing status + focus history
│   │   ├── 01_Story_Title.md
│   │   └── 02_Another_Story.md
│   └── rss.xml               # Per-channel RSS feed
└── rss.xml                   # Aggregate feed (all channels)

state/
├── channels.json             # Channel list
├── queue/push_queue.json     # WebSub incoming video queue
├── run_logs/                 # Daemon log + per-run summaries
└── users/
    ├── index.json            # email→UUID index
    └── <uuid>/
        └── user.json         # Account, subscriptions, prefs, digest state
```

## Web UI Features

- **User accounts** — register, log in, manage subscriptions and display preferences
- **Personal feed** — inbox (unread), read, starred, and all-stories tabs
- **Per-channel topic focus** — up to 3 focus lines per subscription filter AI output
- **Shareable feed page** — public `/feed/<token>.html` and RSS at `/feed/<token>.xml`
- **Daily email digest** — opt-in morning digest via Resend (requires `resend_api_key` in config)
- **Admin panel** — manage channels, users, view run history, trigger manual runs

## Documentation

| File | Contents |
|---|---|
| `CLAUDE.md` | Developer quick reference (architecture, conventions, testing policy) |
| `DEVREF.md` | Full developer reference (function signatures, schemas, route map) |
| `SERVING.md` | Production deployment guide |
| `TODO.md` | Known issues and completed work |
| `helpers/` | Utility scripts: `catchup.py`, `reset_password.py`, `check_quota.py` |

## License

BSD 2-Clause. See `LICENSE`.
