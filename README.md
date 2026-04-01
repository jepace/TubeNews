# TubeNews

Turn any YouTube channel into a personalised news feed.

TubeNews monitors YouTube channels, transcribes new videos via [Supadata](https://supadata.ai), and uses Google Gemini AI to write AP-style news stories from the content. Each user gets a personalised feed filtered to the topics they care about, served through a web UI with subscriptions, an inbox, and shareable feed pages.

## What It Does

1. Discovers new videos on configured YouTube channels
2. Fetches full transcripts (with timestamps) via the Supadata API
3. Sends transcripts to Gemini AI with a journalistic prompt focused on your configured topics
4. Saves AI-generated news stories as Markdown files
5. Serves stories through a web UI; also publishes RSS feeds for feed readers

## Quick Start

```bash
# Install dependencies (global install — no venv needed)
pip install -r requirements.txt

# Configure
cp TubeNews.json.sample TubeNews.json
# Edit TubeNews.json with your API keys and channel list

# Run the scraper
python3 TubeNews.py

# Start the web UI
./serve.sh
# Open http://your-server:8000
```

See `SERVING.md` for production deployment (gunicorn, HTTPS, cron scheduling).

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

## Storage

Stories, feeds, and user data are written to `content/`:

```
content/
├── my_youtube_channel/
│   ├── 2026-03-14_VIDEO-ID/
│   │   ├── transcript.txt
│   │   ├── metadata.json
│   │   ├── 01_Story_Title.md
│   │   └── 02_Another_Story.md
│   └── rss.xml
├── _run_logs/            ← per-run logs and summaries (internal)
├── _users/               ← one directory per registered user (internal)
│   ├── index.json        ← email→UUID index for fast login lookup
│   └── <uuid>/           ← one sub-directory per user account
└── rss.xml               ← aggregated feed
```

## Documentation

- `SERVING.md` — production deployment: gunicorn, HTTPS, cron scheduling
- `CLAUDE.md` — full architecture guide for developers and AI assistants
- `TODO.md` — known issues and maintainability backlog
- `helpers/` — utility scripts for setup, debugging, and migration

## License

BSD 2-Clause. See `LICENSE`.
