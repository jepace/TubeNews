# CLAUDE.md — TubeNews Developer Guide

## Project Summary

**TubeNews** is a Python automation tool that turns YouTube government meeting videos into RSS news feeds. It:

1. Discovers new videos on configured YouTube channels
2. Fetches transcripts via the Supadata API
3. Sends transcripts to Google Gemini AI with a journalistic prompt
4. Saves AI-generated news stories as Markdown files
5. Publishes per-channel RSS feeds and an aggregated regional aggregate feed

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
├── tubenews_utils.py        # Lightweight utils shared between TubeNews.py and helpers/
├── tests/
│   ├── __init__.py
│   └── test_tubenews.py     # pytest unit tests
├── helpers/
│   ├── catchup.py           # Mark all existing videos as "too old" (first-run util)
│   └── check_quota.py       # Test Gemini API key quota across models
└── web/
    ├── app.py               # Flask web UI (user accounts, subscriptions, admin)
    └── templates/           # Jinja2 HTML templates
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
   rebuild_aggregate_feed() ──► archive/rss.xml  (all channels combined)
```

### Key Design Decisions

- **Filesystem as database:** Processed state is stored entirely in `archive/`. No database required.
- **Incremental processing:** A video with an existing `metadata.json` is always skipped.
- **Auto-catchup for new feeds:** When a channel is added for the first time, only the most recent video is processed; the rest are marked `ignored_too_old`.
- **Transcript caching:** If `transcript.txt` already exists in a video directory, Supadata is not called again — AI runs on the cached transcript instead. This allows re-running AI analysis without consuming API quota.
- **Graceful AI degradation:** If Gemini returns HTTP 429 (rate limit), the session flag `ai_disabled` is set and all remaining videos skip the AI step.
- **Shared story parser:** `parse_story_file()` is used by both `rebuild_feed()` and `rebuild_aggregate_feed()` to read the Markdown story format into a structured dict. Edit this function if the story file format changes.
- **Per-user story attribution:** Each story is tagged at write time with the UUIDs of the users whose focus produced it (written as a `**Users:**` line in the `.md` file). At serve time `_get_user_stories()` and `build_user_feed_xml()` show a story only to users whose UUID is in that list. Stories with no `**Users:**` line (feed-level focus or legacy stories) are shown to everyone.
- **Multiple focuses per user:** Users may enter up to 3 focus lines per channel subscription in the dashboard. At processing time, `_collect_channel_focuses()` reads all subscribers' `user.json` files and returns a list of `(focus, user_ids)` pairs (capped at `MAX_FOCUSES_PER_CHANNEL = 10`). Gemini is called once per unique focus. Stories from all focus passes are merged into the same meeting directory, deduplicated by title; user_ids are merged when the same story title appears in multiple focus passes (unrestricted feed-level focus always wins). `metadata.json` records `processed_focuses` so subsequent runs only call Gemini for newly added focuses.

---

## Data Contracts (TypedDicts)

Defined at the top of `TubeNews.py` (and importable into `web/app.py`):

| TypedDict | Fields | Used by |
|---|---|---|
| `VideoInfo` | `id`, `title`, `date`, `is_live` (all `str`/`bool`) | `discover_videos()` return type |
| `FeedConfig` | `channel_id`, `channel_name`, `focus` (all `str`) | Config array entries; `rebuild_feed`, `process_feed`, `process_video` parameters |
| `GeminiStory` | `title`, `dateline`, `content` (`str`), `start_time_seconds` (`int`), `topics` (`list[str]`) | `call_gemini_api()` return type; `write_story_files()` input |
| `ParsedStory` | `title`, `dateline`, `body_html` (`str`), `start_seconds` (`int`), `topics` (`list[str]`), `content_hash` (`str`), `user_ids` (`list[str]`) | `parse_story_file()` return type; imported by `web/app.py` |
| `MetadataDict` | `video_id`, `video_title`, `status`, `processed_at`, `processed_focuses` (`total=False`) | Internal; represents `metadata.json` content |
| `FeedResult` | `channel_id`, `channel_name` (`str`), `stories_written` (`int`) | `_main_body` / `_run_feed` inner dict; `_send_ntfy` parameter |

Defined in `web/app.py`:

| TypedDict | Fields | Used by |
|---|---|---|
| `ChannelInfo` | `channel_id`, `channel_name` (both `str`) | `_channel_info_for_dir()` return type |
| `ChannelStat` | `channel_id`, `channel_name`, `processed`, `ignored`, `no_stories`, `story_count` (`int`), `last_processed` (`float`) | `_archive_channel_stats()` return type |
| `StoryDict` | `title`, `dateline`, `body_html`, `video_id`, `video_title`, `channel_name`, `channel_slug`, `meeting_id`, `story_filename` (`str`), `start_seconds` (`int`), `processed_at` (`float`) | `_get_user_stories()` and `_get_channel_stories()` return type; passed to Flask templates |

---

## Function Reference

### Utility

| Function | Description |
|---|---|
| `slugify(text)` | Converts a string to a filesystem-safe slug (non-alphanumeric → underscore). Defined in `tubenews_utils.py`; re-exported by `TubeNews.py`. |
| `_fmt_no_leading_zeros(dt, fmt)` | Formats a `datetime` with `fmt` and strips POSIX-style leading zeros from day/hour fields. Portable replacement for `%-d`/`%-I`. |
| `parse_story_file(story_path)` | Reads a `.md` story file; returns `ParsedStory` |
| `_story_matches_focus(story_topics, focuses)` | Returns `True` if any story topic overlaps with any of the user's focus strings. *focuses* may be a single string (legacy) or a list of strings (one per focus line). Always `True` when all focuses are empty or `story_topics` is empty. Still used in internal logic; no longer drives serve-time filtering (replaced by `user_ids` attribution). |
| `_needs_processing(video_id, feed_dir)` | Returns `True` if the video has no `metadata.json` in the archive (new video or recovery path). Videos with any `metadata.json` are considered done and are not reprocessed. |
| `_collect_channel_focuses(channel_id, feed_focus)` | Reads `archive/users/*/user.json` and returns a list of `(focus, user_ids)` pairs for *channel_id*. Feed-level *feed_focus* comes first with `user_ids=[]` (unrestricted). User focuses carry the UUIDs of all subscribers who set that focus; if multiple users share a focus their IDs are merged into one entry. Returns `[("", [])]` if nothing is configured (single unrestricted call). Capped at `MAX_FOCUSES_PER_CHANNEL` (10). |

### YouTube data-gathering

| Function | Description |
|---|---|
| `_relative_date_to_iso(text)` | Converts a YouTube relative-date string (e.g. `"11 days ago"`) to `YYYY-MM-DD`; parses exact dates from completed-stream text |
| `_parse_channel_page_metadata(html)` | Extracts `{videoId → {title, date, is_live}}` from the `ytInitialData` JSON blob embedded in a channel listing page |
| `discover_videos(channel_id)` | Scrapes the channel's `videos` and `streams` tabs; returns `list[VideoInfo]` |
| `fetch_transcript(video_id, supadata_client, ..., transcript_rate_limit_event=None)` | Fetches timed transcript segments from Supadata; returns formatted string or None. When a quota-exhausted error is detected (HTTP 402 or `SupadataError` with a credit-related `error` code), sets `transcript_rate_limit_event` before returning None so callers can stop immediately. |

### AI story generation

| Function | Description |
|---|---|
| `call_gemini_api(...)` | Posts to Gemini REST API; returns `list[GeminiStory]`, `False` on rate-limit, `None` on failure |
| `write_story_files(stories, meeting_dir, video_id="", *, clear_existing=True, start_index=1)` | Writes each `GeminiStory` as a numbered `.md` file; includes `**Topics:**` when topics are present and `**Users:**` when the story dict has a non-empty `_user_ids` key (set by `process_video` before calling this function). Use `clear_existing=False, start_index=N` to append new stories from additional focus passes without removing existing files. |

### RSS feed builders

**Naming convention:** `build_*` functions return content (bytes or HTML) and never touch disk — the web app calls these directly to serve feeds and blog pages dynamically. `rebuild_*` functions write to disk and are called by the CLI/scraper to produce static files. `rebuild_user_feed` is a thin wrapper: it calls `build_user_feed_xml` and writes the result to disk.

| Function | Description |
|---|---|
| `rebuild_feed(feed_dir, feed_cfg)` | Generates `archive/<channel>/rss.xml` (all stories) |
| `rebuild_aggregate_feed(base_url)` | Generates `archive/rss.xml` from all channels (all stories) |
| `build_user_feed_xml(user, base_url)` | Builds and returns RSS feed XML bytes for a user's subscribed channels; does **not** write to disk |
| `rebuild_user_feed(user, base_url)` | Thin CLI wrapper: calls `build_user_feed_xml` and writes `archive/users/<slug>/rss.xml` to disk |
| `rebuild_user_blog(user, base_url)` | Generates `archive/users/<slug>/index.html` — a self-contained blog page with stories from subscribed channels |

### Processing orchestration

| Function | Description |
|---|---|
| `process_video(video_id, ..., focuses=None, transcript_rate_limit_event=None)` | Fetch + analyse one video. *focuses* is a list of `(focus_string, user_ids)` pairs; calls Gemini once per pair and writes `**Users:**` metadata for each story. Falls back to `[(feed["focus"], [])]` (unrestricted) when omitted. Deduplicates stories by title across focus passes, merging user_ids. Returns `("content_written", n)`, `("ai_rate_limited", 0)`, `("transcript_quota_exhausted", 0)`, or `("skipped", 0)`. Skips the transcript API call immediately if `transcript_rate_limit_event` is already set. |
| `process_feed(feed, ..., ai_rate_limit_event=None, transcript_rate_limit_event=None)` | Collects focuses via `_collect_channel_focuses`, processes all videos needing work for any focus; returns `(content_changed, ai_rate_limited, stories_written)`. Breaks out of the video loop immediately when `transcript_rate_limit_event` is set. |
| `_check_supadata_quota(config)` | Reads `archive/supadata_balance.json` (written at the end of the previous run) and returns `(ok, balance)`. If `ok` is False, `_main_body` records `transcript_quota_exhausted: True` in the run log and exits without processing any videos. No live API call is made — uses only the cached file. |
| `main()` | Entry point: loads config, calls `process_feed` for each configured channel |

---

## Setup

### Prerequisites
- Python 3.10+
- A [Supadata](https://supadata.ai) account and API key
- A [Google AI Studio](https://aistudio.google.com) Gemini API key

### Install Dependencies

```bash
pip install -r requirements.txt
```

Packages install globally — no virtual environment is used.

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
python3 TubeNews.py

# Debug mode (verbose logging, shows API calls and raw responses)
python3 TubeNews.py --debug

# Start the web server (gunicorn — never use python3 web/app.py in any environment)
./serve.sh
```

### First Run on a Channel with Existing Videos

Run `catchup.py` **before** the first `TubeNews.py` run to avoid processing the entire backlog:

```bash
python3 helpers/catchup.py
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
│   ├── 2000-01-01_XXXXXXXXXXX/ # ignored_too_old stubs use 2000 date prefix
│   │   └── metadata.json       # {status: "ignored_too_old"}
│   └── rss.xml                 # Per-channel RSS feed
└── rss.xml                     # Regional aggregate feed (all channels)
```

### Story Markdown Format

```markdown
# Story Title
*CITY, State — Month DD, YYYY*

Story body text in AP inverted pyramid style...

---
**Segment Start:** 1234s
**Topics:** housing, zoning, permits
```

- `**Segment Start:**` links back to the exact timestamp in the source YouTube video. Parsed by `parse_story_file()` and embedded in RSS entry links as `?t=<seconds>`.
- `**Topics:**` is a comma-separated list of 2–6 lowercase keywords assigned by Gemini. Parsed into `ParsedStory.topics` and used by `_story_matches_focus()`. Not used for serve-time filtering — that role is now handled by `**Users:**`.
- `**Users:**` is a comma-separated list of user UUID directory names. Written by `write_story_files()` when the story was generated for a user-level focus (not the feed-level focus). At serve time, `_get_user_stories()` and `build_user_feed_xml()` skip stories whose `user_ids` list does not include the requesting user's UUID. Absent on feed-level stories and all legacy stories — those are always shown to every subscriber.

### metadata.json Schema

```json
{
  "video_id": "dQw4w9WgXcQ",
  "video_title": "Regular Meeting March 14 2026",
  "status": "processed",
  "processed_at": 1741910400,
  "processed_focuses": ["housing, zoning, permits", "transportation, roads"]
}
```

`status` values: `"processed"` | `"ignored_too_old"` | `"no_stories"` (AI ran but returned no relevant stories)

`processed_focuses` is a sorted list of all focus strings for which Gemini has been called on this video. Old `metadata.json` files that pre-date this field have no `processed_focuses` key and are treated as fully processed (not re-run).

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
| `archive_dir` | No | Path to the archive directory (default: `archive/` next to `TubeNews.py`). Use an absolute path (e.g. `/var/www/html/tubenews`) or a path relative to `TubeNews.py` to point the archive at your web server's document root |
| `request_timeout` | No | Seconds before giving up on YouTube scrape and Supadata API calls (default: `15`). Increase on slow or high-latency connections |
| `base_url` | No | Public URL of `archive/rss.xml`, used as the aggregate feed self-link |
| `ntfy_topic` | No | ntfy.sh topic for run-summary push notifications (e.g. `"TubeNewsAdmin"`); omit to disable |
| `max_parallel_feeds` | No | Max channels processed concurrently (default: `3`; capped at number of feeds) |
| `port` | No | Port the Flask web UI listens on (default: `8000`) |
| `tubenews_key` | Web UI only | Flask session secret key — generate with `python -c 'import secrets; print(secrets.token_hex(32))'`; also readable from `TUBENEWS_SECRET_KEY` env var |
| `admin_users` | No | List of email addresses granted admin access to the web UI (e.g. `["alice@example.com"]`) |

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

| File | Covers |
|---|---|
| `tests/test_tubenews.py` | `slugify`, `parse_story_file` (including `topics` and `user_ids`), `_story_matches_focus`, `write_story_files` (including `**Users:**` output), `_collect_channel_focuses` (tuple return type, user-id merging), the JSON extraction regex in `call_gemini_api`, `rebuild_feed`, `rebuild_aggregate_feed`, `build_user_feed_xml`, lock/unlock helpers, config resolution |
| `tests/test_web.py` | `web/app.py` URL helpers (`_feed_url`, `_blog_url`), user preferences |
| `tests/test_webapp.py` | Flask routes: login guards, dashboard subscription save, admin guards, public token routes, lock-file detection, run-now trigger, channel browse YouTube link, admin runs channel links |

All tests use `tmp_path` fixtures — no network calls and no real archive needed. For functions that hit external APIs (`fetch_transcript`, `call_gemini_api`), use `monkeypatch` or `unittest.mock.patch`.

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
- Return a raw JSON list (no markdown code fences) where each object has keys: `title`, `dateline`, `content`, `start_time_seconds`, `topics`
- `topics` must be a list of 2–6 short lowercase keyword strings categorising the story (used for per-user filtering)

The JSON is extracted with a regex `re.search(r'\[\s*{.*}\s*\]', raw, re.DOTALL)` to handle any extra prose the model may prepend. If the model starts returning malformed JSON, add debug logging inside `call_gemini_api()` to inspect `raw_text` before parsing.

---

## Web Application (`web/app.py`)

The Flask web UI sits on top of `TubeNews.py` and provides user accounts,
subscriptions, and a dashboard for sharing feeds and blog pages. It imports
`build_user_feed_xml`, `parse_story_file`, `slugify`,
and `STORAGE_ROOT` directly from `TubeNews.py`.

### User Storage

Each user is stored as a UUID-named directory under `archive/users/`:

```
archive/users/
└── <uuid>/
    ├── user.json      # account data (see schema below)
    ├── rss.xml        # personal RSS feed (pre-built by CLI; not used by web app)
    └── index.html     # personal blog page (built by rebuild_user_blog; not used by web app)
```

**`user.json` schema:**

```json
{
  "name": "Alice",
  "email": "alice@example.com",
  "password_hash": "scrypt:...",
  "channel_ids": ["UCxxxxxxx", "UCyyyyyyy"],
  "channel_focus": {
    "UCxxxxxxx": ["housing, zoning, permits", "transportation, roads, transit"]
  },
  "feed_token": "550e8400-e29b-41d4-a716-446655440000",
  "created_at": 1741910400,
  "locked": false,
  "seen_channel_ids": ["UCxxxxxxx", "UCyyyyyyy"]
}
```

- `channel_ids` — authoritative subscription list.
- `channel_focus` — optional per-channel focus keywords set by the user on the dashboard. Each value is a **list of strings** (one per focus line, up to 3); old installs may store a plain string — both are handled transparently. Missing key or empty list means no filter (show all stories). `_collect_channel_focuses` reads this at processing time to determine which Gemini calls to make for each channel.
- `feed_token` — UUID generated at registration; authenticates all public (no-login) URLs for that user. Rotating it invalidates both the RSS feed URL and the blog URL simultaneously.
- `seen_channel_ids` — list of channel IDs the user has "seen" on the dashboard. The `inject_body_classes` context processor diffs this against the current feed list to compute `unseen_channel_count`, which drives the red badge on the "Settings" nav link. Key absent means not yet initialised (pre-feature users); treated as 0 unseen so existing users aren't badged on upgrade. Written (covering all current channels) whenever the user loads or saves the dashboard.

### Token Model

One `feed_token` per user covers two public URLs:

| URL | Content |
|---|---|
| `/feed/<token>.xml` | Personal RSS feed |
| `/blog/<token>.html` | Personal blog page |

Both `/feed/<token>.xml` and `/blog/<token>.html` are generated **dynamically
on every request** — no static file is read or written by the web app.
The extension-less variants (`/feed/<token>`, `/blog/<token>`) also work for
backwards compatibility.

### On-request Generation Flow

Both the feed and blog are generated fresh on each request from the live archive:

**RSS feed** (`/feed/<token>.xml`):
1. Token matched to a user in `archive/users/`
2. `build_user_feed_xml()` scans `archive/` and builds feedgen XML in memory
3. XML bytes returned directly as the HTTP response — nothing written to disk

**Blog** (`/blog/<token>.html` and logged-in `/blog`):
1. `_get_user_stories()` scans `archive/` and returns all stories from the user's
   subscribed channels
2. Flask renders `blog.html` with the story list and returns HTML

Because both read the archive on every request, they always reflect the latest
stories without any explicit rebuild step.

`rebuild_user_feed()` and `rebuild_user_blog()` exist in `TubeNews.py` as
standalone utilities (used by the CLI, or for generating static snapshots) but
the web app does **not** call either — the web UI uses dynamic generation only.

### Key Helpers

| Function | Description |
|---|---|
| `_load_config()` | Reads `TubeNews.json`; returns `{}` on failure |
| `_save_feeds(feeds)` | Writes updated `feeds` list back to `TubeNews.json` |
| `_load_channels()` | Returns the `feeds` list from config |
| `_base_url()` | Returns `base_url` from config (empty string if not set) |
| `_feed_url(token)` | Builds the full `/feed/<token>.xml` URL using `base_url` or `url_for` |
| `_find_archive_dir_for_channel(channel_id)` | Scans `archive/*/channel.json` and returns the `Path` whose `channel_id` matches; used by `admin_feed_edit` to locate the archive dir regardless of historical directory naming |
| `_blog_url(token)` | Builds the full `/blog/<token>.html` URL using `base_url` or `url_for` |
| `_read_email_index()` | Returns the email→UUID dict from `archive/users/index.json`; returns `{}` on any error |
| `_write_email_index(index)` | Atomically writes the email→UUID dict to `archive/users/index.json` (write-then-rename) |
| `_index_add(email, uid)` | Adds or updates an entry in the email index |
| `_index_remove(email)` | Removes an entry from the email index |
| `_find_user_by_email(email)` | O(1) lookup via `index.json`; falls back to a glob scan and repairs the index if the entry is missing or stale |
| `_find_user_by_id(uid)` | Loads a user by their UUID directory name |
| `_all_users()` | Returns all users sorted by name |

### Route Map

**No authentication required:**

| Method | Route | Handler | Description |
|---|---|---|---|
| GET | `/` | `index` | Redirects to dashboard or login |
| GET/POST | `/login` | `login` | Login form (rate-limited: 10/min) |
| GET/POST | `/register` | `register` | Registration form (rate-limited: 5/min) |
| GET | `/archive/<path>` | `serve_archive` | Serves files from `archive/` directory |
| GET | `/feed/<token>.xml` | `serve_feed` | Personal RSS feed by token |
| GET | `/blog/<token>.html` | `serve_blog_public` | Personal blog page by token |

**Login required:**

| Method | Route | Handler | Description |
|---|---|---|---|
| GET/POST | `/dashboard` | `dashboard` | Subscribe to channels; shows feed and blog URLs |
| GET | `/logout` | `logout` | Clears session |
| GET | `/blog` | `serve_blog` | Regenerates and serves the logged-in user's blog |
| GET | `/channel/<channel_id>` | `channel_blog` | Browse all stories for one channel (no time cutoff); passes `channel_id` to `blog.html` so the sub-header can link to the YouTube channel page |

**Admin required (`admin_users` in config):**

| Method | Route | Handler | Description |
|---|---|---|---|
| GET | `/admin` | `admin_users` | User list |
| GET | `/admin/user/<uid>` | `admin_user` | User detail / edit |
| POST | `/admin/user/<uid>/info` | `admin_user_info` | Update name and email |
| POST | `/admin/user/<uid>/subscriptions` | `admin_user_subscriptions` | Update channel subscriptions |
| POST | `/admin/user/<uid>/password` | `admin_user_password` | Reset password |
| POST | `/admin/user/<uid>/lock` | `admin_user_lock` | Toggle account lock |
| POST | `/admin/user/<uid>/rotate-token` | `admin_rotate_token` | Issue new feed token (invalidates old URLs) |
| POST | `/admin/user/<uid>/delete` | `admin_user_delete` | Delete account (requires email confirmation) |
| GET | `/admin/feeds` | `admin_feeds` | Feed (channel) list |
| GET/POST | `/admin/feeds/add` | `admin_feed_add` | Add a channel to config |
| GET/POST | `/admin/feeds/<channel_id>/edit` | `admin_feed_edit` | Edit a channel in config; renames the archive directory when `channel_name` changes so the back catalog is preserved |
| POST | `/admin/feeds/<idx>/delete` | `admin_feed_delete` | Remove a channel from config |
| GET | `/admin/blog` | `admin_all_stories` | Blog view of all stories from all channels — the HTML counterpart to `archive/rss.xml`; links to that aggregate feed in the sub-header |
| POST | `/admin/story/delete` | `admin_story_delete` | Delete a single story `.md` file; rebuilds the per-channel and aggregate feeds; only accepts numbered `.md` filenames (path traversal guarded) |

### Sticky Sub-Header Row

`base.html` exposes a `header_sub` Jinja block. When a child template overrides the block with content, a 36 px sticky band (`position: sticky; top: 52px; z-index: 190`) appears immediately below the main navigation header and stays visible while scrolling. When the block is left empty (the default), nothing is rendered and no space is reserved.

**Current uses:**
- `blog.html` always renders a sub-header containing the feed/page name. When the template receives a `channel_id` variable (set only by `channel_blog()`), a "▶ YouTube channel" link is added.

**Sizing note:** The sub-header's `top: 52px` assumes the main header is exactly 52 px tall (set by `header { height: 52px }` in `style.css`). The transcript banner uses the same `top: 52px` on a page that has no sub-header, so there is no conflict. If the main header height ever changes, update both `top` values together.

### Security Notes

- All state-changing routes use CSRF tokens (flask-wtf).
- Login and register routes are rate-limited (flask-limiter).
- `SESSION_COOKIE_SECURE` is only set when `TUBENEWS_HTTPS=true` is in the
  environment, so local dev works without HTTPS.
- Admins are determined solely by email match against `admin_users` in
  `TubeNews.json` — there is no `is_admin` flag stored in `user.json`.
- Locked accounts (`"locked": true`) fail `is_active` and are rejected by
  flask-login on every request without needing to log out.

---

## Testing Policy

**Writing tests is an ongoing responsibility, not a one-time task.**

- **Tests ship with the code change.** Any new or modified function must have corresponding tests in the same commit. Do not defer tests to a follow-up.
- **Run the full suite before every commit.** `pytest tests/ -v` must pass before pushing. A commit that breaks existing tests must not be pushed.
- **Bug fixes require a regression test.** Add a test that would have caught the original bug before closing the fix. If the same class of bug appears twice, the suite was insufficient — expand it.

---

## Documentation Policy

**Documentation must be kept in sync with the code. Outdated docs are worse than no docs.**

- **Update docs in the same commit as the code change.** Adding a function means adding it to the Function Reference. Changing a file format means updating the schema example. Changing behaviour means updating the description.
- **Files to maintain:**
  - `CLAUDE.md` — developer reference (function signatures, data formats, design decisions, policies)
  - `SERVING.md` — operator/deployment guide (URLs, config, infrastructure)
  - `TODO.md` — known issues, deferred work, and completed items
  - `README.md` — user-facing overview and quick-start
- **Completed work belongs in `TODO.md`.** When a deferred item is implemented, move it to a Completed section rather than deleting it — this preserves the design rationale.
- **Do not document hypothetical features.** Only document what actually exists in the codebase.

---

## URL Generation Rules

**Never use `_external=True` with `url_for()`** in the web UI. The deployment does not yet have HTTPS configured, and `_external=True` produces absolute URLs (with scheme and host) that break when the scheme or host is wrong.

- `_feed_url(token)` and `_blog_url(token)` return relative paths (`/feed/<token>.xml`, `/blog/<token>.html`) when `base_url` is not set in `TubeNews.json`. They only prepend an absolute base when the operator has explicitly configured `base_url`.
- All links rendered in HTML templates must be relative (use `url_for()` without `_external=True`, or hardcoded root-relative paths like `/feed/...`).
- The only place absolute URLs are appropriate is inside generated RSS/Atom XML, and only when `base_url` is configured.

---

## Commit & Branch Conventions

This project uses descriptive commit messages. When working on this repo as an AI assistant, push to the branch specified in the task instructions. Never push to `main` directly.
