# TubeNews

Automated local news extraction from YouTube government meeting videos.

TubeNews monitors YouTube channels for new videos, fetches transcripts via [Supadata](https://supadata.ai), analyzes them with Google Gemini AI using a journalistic prompt, and publishes per-channel RSS feeds plus an aggregated regional meta-feed.

## What It Does

1. Discovers new videos on configured YouTube channels
2. Fetches full transcripts (with timestamps) via the Supadata API
3. Sends transcripts to Gemini AI with an investigative-reporter prompt focused on your configured topics
4. Saves professional AP-style news stories as Markdown files
5. Generates RSS feeds you can subscribe to in any feed reader

## Quick Start

```bash
# Install dependencies
pip install -r requirements.txt

# Configure
cp TubeNews.json.sample TubeNews.json
# Edit TubeNews.json with your API keys and channel list

# (Optional) On first run with existing channels — skip the backlog
python helpers/catchup.py

# Run
python TubeNews.py
python TubeNews.py --debug   # verbose output
```

## Requirements

- Python 3.8+
- [Supadata API key](https://supadata.ai) — for transcript extraction
- [Google Gemini API key](https://aistudio.google.com) — for AI story generation

## Configuration

See `TubeNews.json.sample` for the full template. Key fields:

```json
{
  "gemini_api_key": "YOUR_KEY",
  "ai_model": "gemini-2.5-flash",
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

## Output

RSS feeds are written to `archive/`:

```
archive/
├── my_youtube_channel/
│   ├── 2026-03-14_VIDEO-ID/
│   │   ├── transcript.txt
│   │   ├── metadata.json
│   │   ├── 01_Story_Title.md
│   │   └── 02_Another_Story.md
│   └── rss.xml           ← subscribe to this
└── rss.xml               ← aggregated regional feed
```

## Documentation

- `CLAUDE.md` — full architecture guide for developers and AI assistants
- `TODO.md` — known issues and maintainability backlog
- `helpers/` — utility scripts for setup, debugging, and migration

## License

BSD 2-Clause. See `LICENSE`.
