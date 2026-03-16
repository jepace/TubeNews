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
├── TubeNews.py              # Main application (single-file, ~280 lines)
├── TubeNews.json            # Runtime config (gitignored — copy from .sample)
├── TubeNews.json.sample     # Config template
├── requirements.txt         # Python dependencies
├── README.md
├── CLAUDE.md                # This file
├── TODO.md                  # Known issues and maintainability backlog
├── LICENSE
├── .gitignore
└── helpers/
    ├── catchup.py           # Mark all existing videos as "too old" (first-run util)
    └── check_quota.py       # Test Gemini API key quota across models
```

---

## Architecture & Data Flow

```
YouTube Channel Pages (HTML scrape)
         │
         ▼  video IDs
  discover_video_ids()
         │
         ▼  new video IDs only
  get_transcript_and_meta()  ──► Supadata API
         │
         ▼  transcript text + metadata
    [save to archive/<council>/<date>_<id>/transcript.txt]
         │
         ▼
   generate_news()  ──► Google Gemini API
         │
         ▼  JSON list of stories
    [save as 01_title.md, 02_title.md, ...]
    [save metadata.json]
         │
         ▼
   rebuild_feed()   ──► archive/<council>/rss.xml
         │
         ▼
   rebuild_meta_feed() ──► archive/rss.xml  (all councils combined)
```

### Key Design Decisions
- **Filesystem as database:** Processed state is stored entirely in `archive/`. No database required.
- **Incremental processing:** A video with an existing `metadata.json` is always skipped.
- **Auto-catchup for new feeds:** When a channel is added for the first time, only the most recent video is processed; the rest are marked `ignored_too_old`.
- **Transcript caching:** If `transcript.txt` already exists in a video directory, Supadata is not called again — the AI runs on the cached transcript instead.
- **Graceful AI degradation:** If Gemini returns HTTP 429 (rate limit), `AI_DISABLED` is set and the rest of the run continues without AI calls.

---

## Setup

### Prerequisites
- Python 3.8+
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
  "ai_model": "gemini-2.5-flash",
  "supadata_api_key": "YOUR_SUPADATA_KEY",
  "feeds": [
    {
      "channel_id": "UCxxxxxxxxxxxxxxxxxxxxxxx",
      "channel_name": "City Council Name",
      "focus": "housing, zoning, development permits, budget decisions"
    }
  ]
}
```

> **Note:** `output_dir` appears in the sample config but is currently unused — the app always writes to `archive/` in the project root.

---

## Running

```bash
# Normal run
python TubeNews.py

# Debug mode (verbose logging)
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
├── city_council_name/          # slugified channel_name
│   ├── 2026-03-14_dQw4w9WgXcQ/
│   │   ├── transcript.txt      # Raw Supadata output (SECONDS --> TEXT)
│   │   ├── metadata.json       # {video_id, video_title, status, processed_at}
│   │   ├── 01_Story_Title.md
│   │   └── 02_Another_Story.md
│   ├── 1900-01-01_XXXXXXXXXXX/ # Ignored/backlog videos use 1900 date
│   │   └── metadata.json       # {status: "ignored_too_old"}
│   └── rss.xml                 # Per-council RSS feed (up to 50 stories)
└── rss.xml                     # Regional meta-feed (up to 100 stories)
```

### Story Markdown Format

```markdown
# Story Title
*CITY, State — Month DD, YYYY*

Story body text in AP inverted pyramid style...

---
**Segment Start:** 1234s
```

The `**Segment Start:**` value links back to the exact timestamp in the source YouTube video.

### metadata.json Schema

```json
{
  "video_id": "dQw4w9WgXcQ",
  "video_title": "Regular Council Meeting March 14 2026",
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
| `ai_model` | Yes | Gemini model name (e.g. `gemini-2.5-flash`) |
| `supadata_api_key` | Yes | Supadata API key for transcript fetching |
| `feeds` | Yes | Array of channel configurations (see below) |
| `feeds[].channel_id` | Yes | YouTube channel ID (starts with `UC`) |
| `feeds[].channel_name` | Yes | Human-readable name; used to create `archive/` subfolder |
| `feeds[].focus` | Yes | Topic guidance for the AI (e.g. "housing, zoning, permits") |
| `output_dir` | No | Unused — present in sample for future use |

---

## Helper Scripts

| Script | Purpose | When to Use |
|---|---|---|
| `helpers/catchup.py` | Marks all visible videos as ignored | Before first run on a channel with existing videos |
| `helpers/check_quota.py` | Tests Gemini API key quota across models | When AI calls fail with 429 errors |

---

## Development Notes

### External Dependencies and Fragility
- **YouTube HTML scraping** (`discover_video_ids`) uses a regex against YouTube's page HTML. YouTube can change this structure at any time, causing silent empty results. Watch for unexpectedly zero new videos.
- **Supadata API** is a paid proxy service. Check account quota if transcripts stop working.
- **Gemini API** has rate limits per project. Use `helpers/check_quota.py` to test. If one project is exhausted, create a new Google Cloud project and generate a fresh key.

### SSL on FreeBSD
Line 13 of `TubeNews.py` sets a FreeBSD-specific SSL cert path. This is guarded by `os.path.exists` — see TODO.md for the current state of this guard.

### AI Prompt Engineering
The Gemini prompt is in `generate_news()` (lines 69–79). It instructs the model to:
- Identify stories relevant to the configured `focus`
- Use AP-style inverted pyramid structure
- Format a dateline (e.g., `CITY, State — Month DD, YYYY`)
- Return a raw JSON list (no markdown code fences)

The JSON is extracted with a regex `re.search(r'\[\s*{.*}\s*\]', raw, re.DOTALL)` to handle any extra prose the model may prepend.

### Known Bugs
See `TODO.md` for a full list. The most impactful bug is in `rebuild_feed()` line 152 — a reference to `html_body` which is never defined (dead code branch that always uses the fallback `body_text`).

### No Formal Test Suite
Tests are manual integration scripts in `helpers/`. There are no unit tests. See TODO.md for the recommendation to add pytest coverage.

---

## Commit & Branch Conventions

This project uses descriptive commit messages. When working on this repo as an AI assistant, push to the branch specified in the task instructions. Never push to `main` directly.
