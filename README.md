# TubeNews

Turn any YouTube channel into an AI-generated news feed.

TubeNews monitors YouTube channels for new videos, fetches transcripts via [Supadata](https://supadata.ai), analyzes them with Google Gemini AI using a journalistic prompt, and publishes per-channel RSS feeds plus an aggregated feed.

## What It Does

1. Discovers new videos on configured YouTube channels
2. Fetches full transcripts (with timestamps) via the Supadata API
3. Sends transcripts to Gemini AI with a journalistic prompt focused on your configured topics
4. Saves professional AP-style news stories as Markdown files
5. Generates RSS feeds you can subscribe to in any feed reader

## Quick Start

```bash
# Install dependencies (global install — no venv needed)
pip install -r requirements.txt

# Configure
cp TubeNews.json.sample TubeNews.json
# Edit TubeNews.json with your API keys and channel list

# Run
python3 TubeNews.py
```

## Requirements

- Python 3.10+
- [Supadata API key](https://supadata.ai) — for transcript extraction
- [Google Gemini API key](https://aistudio.google.com) — for AI story generation

## Configuration

See `TubeNews.json.sample` for the full template. Key fields:

```json
{
  "gemini_api_key": "YOUR_KEY",
  "gemini_model": "gemini-2.5-flash",
  "supadata_api_key": "YOUR_KEY",
  "feeds": [
    {
      "channel_id": "UCxxxxxxxxxxxxxxxxxxxxxxx",
      "channel_name": "My YouTube Channel",
      "focus": "housing, zoning, permits, budget"
    }
  ]
}
```

## Web UI

TubeNews includes a Flask web app (`web/app.py`) that provides user accounts,
channel subscriptions, personalised RSS feeds, shareable blog pages, and an
admin panel for managing users and channels.

```bash
# Add a secret key to TubeNews.json first:
python3 -c 'import secrets; print(secrets.token_hex(32))'
# Then start the server:
./serve.sh
# Open http://your-server:8000
```

See `SERVING.md` for production deployment (gunicorn, nginx, HTTPS, cron).

## Output

RSS feeds and stories are written to `content/`:

```
content/
├── my_youtube_channel/
│   ├── 2026-03-14_VIDEO-ID/
│   │   ├── transcript.txt
│   │   ├── metadata.json
│   │   ├── 01_Story_Title.md
│   │   └── 02_Another_Story.md
│   ├── channel.json      ← channel ID/name mapping
│   └── rss.xml           ← subscribe to this
├── _run_logs/            ← per-run logs and summaries (internal)
│   ├── run_log.json      ← last 30 run summaries
│   └── run-<pid>.log     ← full output for each run
├── _users/
│   └── <uuid>/           ← one directory per registered user
│       └── user.json
└── rss.xml               ← aggregated feed
```

## Documentation

- `SERVING.md` — production deployment: gunicorn, HTTPS, cron scheduling
- `CLAUDE.md` — full architecture guide for developers and AI assistants
- `TODO.md` — known issues and maintainability backlog
- `helpers/` — utility scripts for setup, debugging, and migration

## License

BSD 2-Clause. See `LICENSE`.
