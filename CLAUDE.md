# CLAUDE.md — TubeNews Developer Guide

## Project Summary

**TubeNews** is a Python automation tool that turns YouTube government meeting videos into RSS news feeds. It:

1. Discovers new videos on configured YouTube channels
2. Fetches transcripts via the Supadata API
3. Sends transcripts to Google Gemini AI with a journalistic prompt
4. Saves AI-generated news stories as Markdown files
5. Publishes per-channel RSS feeds and an aggregated regional meta-feed

**Target use case:** Helping citizens stay informed about local government decisions without watching hours of footage.

**Author:** James E. Pace | **License:** BSD 2-Clause

---

## Repository Layout

```
TubeNews/
├── TubeNews.py              # Main application (single-file)
├── TubeNews.json            # Runtime config (gitignored — copy from .sample)
├── TubeNews.json.sample     # Config template
├── requirements.txt         # Python dependencies
├── README.md
├── CLAUDE.md                # This file
├── TODO.md                  # Known issues and maintainability backlog
├── LICENSE
├── .gitignore
├── tests/
│   ├── __init__.py
│   └── test_tubenews.py     # pytest unit tests
└── helpers/
    ├── catchup.py           # Mark all existing videos as "too old" (first-run util)
    └── check_quota.py       # Test Gemini API key quota across models
```

---

## Architecture & Data Flow

```
YouTube Channel Pages (HTML scrape)
         │
         ▼  list of {id, title, date, is_live}
  discover_videos()
         │  (parses ytInitialData JSON embedded in channel page;
         │   title and approximate date extracted here — no per-video watch-page request)
         │
         ▼  IDs not yet in archive
  process_feed() ──► process_video() (one per new video)
         │
         └── fetch_transcript()  ──► Supadata API
                   returns transcript string
         │
         ▼  transcript saved to archive/<channel>/<date>_<id>/transcript.txt
         │
  call_gemini_api()  ──► Google Gemini REST API
         │
         ▼  list of story dicts
  write_story_files()  ──► 01_title.md, 02_title.md, ...
  [saves metadata.json]
         │
         ▼
   rebuild_feed()      ──► archive/<channel>/rss.xml
         │
         ▼
   rebuild_meta_feed() ──► archive/rss.xml  (all channels combined)
```

### Key Design Decisions

- **Filesystem as database:** Processed state is stored entirely in `archive/`. No database required.
- **Incremental processing:** A video with an existing `metadata.json` is always skipped.
- **Auto-catchup for new feeds:** When a channel is added for the first time, only the most recent video is processed; the rest are marked `ignored_too_old`.
- **Transcript caching:** If `transcript.txt` already exists in a video directory, Supadata is not called again — AI runs on the cached transcript instead. This allows re-running AI analysis without consuming API quota.
- **Graceful AI degradation:** If Gemini returns HTTP 429 (rate limit), the session flag `ai_disabled` is set and all remaining videos skip the AI step.
- **Shared story parser:** `parse_story_file()` is used by both `rebuild_feed()` and `rebuild_meta_feed()` to read the Markdown story format into a structured dict. Edit this function if the story file format changes.

---

## Function Reference

### Utility

| Function | Description |
|---|---|
| `slugify(text)` | Converts a string to a filesystem-safe slug (non-alphanumeric → underscore) |
| `parse_story_file(story_path)` | Reads a `.md` story file; returns `{title, dateline, body_html, start_seconds, content_hash}` |

### YouTube data-gathering

| Function | Description |
|---|---|
| `_relative_date_to_iso(text)` | Converts a YouTube relative-date string (e.g. `"11 days ago"`) to `YYYY-MM-DD`; parses exact dates from completed-stream text |
| `_parse_channel_page_metadata(html)` | Extracts `{videoId → {title, date, is_live}}` from the `ytInitialData` JSON blob embedded in a channel listing page |
| `discover_videos(channel_id)` | Scrapes the channel's `videos` and `streams` tabs; returns `list[{id, title, date, is_live}]` |
| `fetch_transcript(video_id, supadata_client)` | Fetches timed transcript segments from Supadata; returns formatted string or None |

### AI story generation

| Function | Description |
|---|---|
| `call_gemini_api(...)` | Posts to Gemini REST API; returns list of story dicts, `False` on rate-limit, `None` on failure |
| `write_story_files(stories, meeting_dir)` | Writes each story dict as a numbered `.md` file |

### RSS feed builders

| Function | Description |
|---|---|
| `rebuild_feed(feed_dir, feed_cfg)` | Generates `archive/<channel>/rss.xml` (all stories) |
| `rebuild_meta_feed(base_url)` | Generates `archive/rss.xml` from all channels (all stories) |
| `rebuild_user_feed(user, base_url)` | Generates `archive/users/<slug>/rss.xml` filtered to a user's subscribed channels |
| `rebuild_user_blog(user, base_url)` | Generates `archive/users/<slug>/index.html` — a self-contained blog page with stories from subscribed channels |

### Processing orchestration

| Function | Description |
|---|---|
| `mark_video_as_backlog(feed_dir, video_id)` | Writes a `2000-01-01_<id>` stub so the video is skipped on future runs |
| `process_video(video_id, ...)` | Fetch + analyse one video; returns `"content_written"`, `"ai_rate_limited"`, or `"skipped"` |
| `process_feed(feed, ...)` | Processes all new videos for one channel; returns `(content_changed, ai_rate_limited)` |
| `main()` | Entry point: loads config, calls `process_feed` for each configured channel |

---

## Setup

### Prerequisites
- Python 3.10+
- A [Supadata](https://supadata.ai) account and API key
- A [Google AI Studio](https://aistudio.google.com) Gemini API key

### Install Dependencies

```bash
python -m venv venv
source venv/bin/activate   # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

### Configure

```bash
cp TubeNews.json.sample TubeNews.json
```

Edit `TubeNews.json`:

```json
{
  "gemini_api_key": "YOUR_GEMINI_KEY",
  "gemini_model": "gemini-2.5-flash",
  "supadata_api_key": "YOUR_SUPADATA_KEY",
  "base_url": "",
  "feeds": [
    {
      "channel_id": "UCxxxxxxxxxxxxxxxxxxxxxxx",
      "channel_name": "My YouTube Channel",
      "focus": "housing, zoning, development permits, budget decisions"
    }
  ]
}
```

---

## Running

```bash
# Normal run
python TubeNews.py

# Debug mode (verbose logging, shows API calls and raw responses)
python TubeNews.py --debug
```

### First Run on a Channel with Existing Videos

Run `catchup.py` **before** the first `TubeNews.py` run to avoid processing the entire backlog:

```bash
python helpers/catchup.py
```

This marks all currently visible videos as `ignored_too_old`. The main script will then only pick up truly new uploads going forward.

---

## Storage Layout

```
archive/
├── city_channel_name/          # slugified channel_name
│   ├── 2026-03-14_dQw4w9WgXcQ/
│   │   ├── transcript.txt      # Raw Supadata output (SECONDS --> TEXT)
│   │   ├── metadata.json       # {video_id, video_title, status, processed_at}
│   │   ├── 01_Story_Title.md
│   │   └── 02_Another_Story.md
│   ├── 2000-01-01_XXXXXXXXXXX/ # Ignored/backlog videos use 2000 date
│   │   └── metadata.json       # {status: "ignored_too_old"}
│   └── rss.xml                 # Per-channel RSS feed
└── rss.xml                     # Regional meta-feed (all channels)
```

### Story Markdown Format

```markdown
# Story Title
*CITY, State — Month DD, YYYY*

Story body text in AP inverted pyramid style...

---
**Segment Start:** 1234s
```

The `**Segment Start:**` value links back to the exact timestamp in the source YouTube video. It is parsed by `parse_story_file()` and embedded in RSS entry links as `?t=<seconds>`.

### metadata.json Schema

```json
{
  "video_id": "dQw4w9WgXcQ",
  "video_title": "Regular Meeting March 14 2026",
  "status": "processed",
  "processed_at": 1741910400
}
```

`status` values: `"processed"` | `"ignored_too_old"`

---

## Configuration Reference

| Key | Required | Description |
|---|---|---|
| `gemini_api_key` | Yes | Google Gemini API key from AI Studio |
| `gemini_model` | Yes | Gemini model name (e.g. `gemini-2.5-flash`) |
| `supadata_api_key` | Yes | Supadata API key for transcript fetching |
| `feeds` | Yes | Array of channel configurations (see below) |
| `feeds[].channel_id` | Yes | YouTube channel ID (starts with `UC`) |
| `feeds[].channel_name` | Yes | Human-readable name; used to create `archive/` subfolder |
| `feeds[].focus` | Yes | Topic guidance for the AI (e.g. "housing, zoning, permits") |
| `base_url` | No | Public URL of `archive/rss.xml`, used as the meta-feed self-link |

---

## Helper Scripts

| Script | Purpose | When to Use |
|---|---|---|
| `helpers/catchup.py` | Marks all visible videos as ignored | Before first run on a channel with existing videos |
| `helpers/check_quota.py` | Tests Gemini API key quota across models | When AI calls fail with 429 errors |

---

## Running Tests

```bash
pytest tests/ -v
```

Tests cover: `slugify`, `parse_story_file`, the JSON extraction regex used by `call_gemini_api`, `rebuild_feed`, and `rebuild_meta_feed`. All tests use `tmp_path` fixtures — no network calls and no real archive needed.

To add a test for a new function, follow the patterns in `tests/test_tubenews.py`. For functions that hit external APIs (`fetch_transcript`, `call_gemini_api`), use `monkeypatch` or `unittest.mock.patch` to avoid live API calls.

---

## Development Notes

### External Dependencies and Fragility

- **YouTube HTML scraping** (`discover_videos`, `_parse_channel_page_metadata`) parses the `ytInitialData` JSON blob embedded in channel listing pages. YouTube can change this structure at any time. If videos stop being discovered or titles/dates stop appearing, check whether the `videoId`, `title.runs`, or `publishedTimeText` JSON paths have changed. The simple `videoId` regex fallback ensures IDs are still found even if the richer metadata parse fails.
- **Approximate dates:** The channel listing page only provides relative dates ("11 days ago"). `_relative_date_to_iso` converts these to approximate calendar dates by subtracting from today. Completed livestreams often include the exact date in the text ("Streamed live on Mar 14, 2026") and are parsed precisely.
- **Supadata API** is a paid proxy service. Check account quota if transcripts stop working. The `Transcript` object returned by `supadata_client.transcript()` exposes only `content`, `lang`, and `available_langs`.
- **Gemini API** has rate limits per project. Use `helpers/check_quota.py` to test. If one project is exhausted, create a new Google Cloud project and generate a fresh key.

### SSL on FreeBSD

`TubeNews.py` sets a FreeBSD-specific SSL cert path near the top. The assignment is guarded by `os.path.exists()` so it is a no-op on Linux and macOS.

### AI Prompt Engineering

The Gemini prompt is in `call_gemini_api()`. It instructs the model to:
- Identify stories relevant to the configured `focus`
- Use AP-style inverted pyramid structure
- Format a dateline (e.g., `CITY, State — Month DD, YYYY`)
- Return a raw JSON list (no markdown code fences)

The JSON is extracted with a regex `re.search(r'\[\s*{.*}\s*\]', raw, re.DOTALL)` to handle any extra prose the model may prepend. If the model starts returning malformed JSON, add debug logging inside `call_gemini_api()` to inspect `raw_text` before parsing.

---

## Commit & Branch Conventions

This project uses descriptive commit messages. When working on this repo as an AI assistant, push to the branch specified in the task instructions. Never push to `main` directly.
