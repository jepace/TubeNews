# CLAUDE.md — TubeNews Developer Guide

## Project Summary

**TubeNews** is a Python automation tool that turns YouTube videos into RSS news feeds. It:

1. Discovers new videos on configured YouTube channels
2. Fetches transcripts via the Supadata API
3. Sends transcripts to Google Gemini AI with a journalistic prompt
4. Saves AI-generated news stories as Markdown files
5. Publishes per-channel RSS feeds and an aggregated feed

**Author:** James E. Pace | **License:** BSD 2-Clause

---

## Repository Layout

```
TubeNews/
├── TubeNews.py              # Main application (single-file)
├── TubeNews.json            # Runtime config (gitignored — copy from .sample)
├── TubeNews.json.sample     # Config template
├── channels.json.sample     # Channel config template (copied to state/channels.json)
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
│   ├── check_quota.py       # Test Gemini API key quota across models
│   ├── dump_channel_html.py # Dump raw ytInitialData JSON (legacy debug tool — no longer used by main scraper)
│   └── reset_password.py    # Emergency CLI password reset (for locked-out admins)
└── web/
    ├── app.py               # Flask web UI (user accounts, subscriptions, admin)
    └── templates/           # Jinja2 HTML templates
```

---

## Architecture & Data Flow

```
YouTube channel Atom RSS feed
         │
         ▼  list of {id, title, date}
  discover_videos()
         │  (fetches https://www.youtube.com/feeds/videos.xml?channel_id=CHANNEL_ID;
         │   returns up to 15 most-recent entries with exact ISO dates)
         │
         ▼  IDs not yet in content store
  process_feed() ──► process_video() (one per new video)
         │
         └── fetch_transcript()  ──► Supadata API
                   returns transcript string
         │
         ▼  transcript saved to content/<channel>/<date>_<id>/transcript.txt
         │
  call_gemini_api()  ──► Google Gemini REST API
         │
         ▼  list of story dicts
  write_story_files()  ──► 01_title.md, 02_title.md, ...
  [saves metadata.json]
         │
         ▼
   rebuild_feed()      ──► content/<channel>/rss.xml
         │
         ▼
   rebuild_aggregate_feed() ──► content/rss.xml  (all channels combined)
```

### Key Design Decisions

- **Filesystem as database:** Story content is stored in `content/`; internal state (users, run logs, channel config, lock file) is stored in the sibling `state/` directory. No database required.
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
| `VideoInfo` | `id`, `title`, `date` (all `str`) | `discover_videos()` return type |
| `FeedConfig` | `channel_id`, `channel_name`, `focus` (all `str`) | Config array entries; `rebuild_feed`, `process_feed`, `process_video` parameters |
| `GeminiStory` | `title`, `dateline`, `content` (`str`), `start_time_seconds` (`int`), `topics` (`list[str]`) | `call_gemini_api()` return type; `write_story_files()` input |
| `ParsedStory` | `title`, `dateline`, `body_html` (`str`), `start_seconds` (`int`), `topics` (`list[str]`), `content_hash` (`str`), `user_ids` (`list[str]`) | `parse_story_file()` return type; imported by `web/app.py` |
| `MetadataDict` | `video_id`, `video_title`, `status`, `processed_at`, `processed_focuses` (`total=False`) | Internal; represents `metadata.json` content |
| `FeedResult` | `channel_id`, `channel_name` (`str`), `stories_written` (`int`) | `_main_body` / `_run_feed` inner dict; `_send_ntfy` parameter. Each run record written to `state/run_logs/run_log.json` also includes a top-level `"pid"` field (`int`, `os.getpid()`) so the web UI can link to `state/run_logs/run-<pid>.log`. |

Defined in `web/app.py`:

| TypedDict | Fields | Used by |
|---|---|---|
| `ChannelInfo` | `channel_id`, `channel_name` (both `str`) | `_channel_info_for_dir()` return type |
| `ChannelStat` | `channel_id`, `channel_name`, `processed`, `ignored`, `no_stories`, `story_count` (`int`), `last_processed` (`float`) | `_archive_channel_stats()` return type |
| `StoryDict` | `title`, `dateline`, `body_html`, `video_id`, `video_title`, `channel_name`, `channel_slug`, `meeting_id`, `story_filename`, `channel_id` (`str`), `start_seconds` (`int`), `processed_at` (`float`), `content_hash` (`str`) | `_get_user_stories()` and `_get_channel_stories()` return type; passed to Flask templates |

---

## Function Reference

### Utility

| Function | Description |
|---|---|
| `slugify(text)` | Converts a string to a filesystem-safe slug (non-alphanumeric → underscore). Defined in `tubenews_utils.py`; re-exported by `TubeNews.py`. |
| `resolve_roots(config_file, base_dir)` | Reads `content_dir` and `state_dir` from the JSON config at *config_file*, resolves relative paths against *base_dir*, and returns `(STORAGE_ROOT, STATE_ROOT)`. Falls back to `base_dir/content` and `base_dir/state` when the keys are absent. Defined in `tubenews_utils.py`. |
| `_fmt_no_leading_zeros(dt, fmt)` | Formats a `datetime` with `fmt` and strips POSIX-style leading zeros from day/hour fields. Portable replacement for `%-d`/`%-I`. |
| `parse_story_file(story_path)` | Reads a `.md` story file; returns `ParsedStory`. Body lines are HTML-escaped with `html.escape()` before joining, so `body_html` is safe for `{{ ... \| safe }}` rendering. |
| `_story_matches_focus(story_topics, focuses)` | Returns `True` if any story topic overlaps with any of the user's focus strings. *focuses* is a list of strings (one per focus line). Always `True` when all focuses are empty or `story_topics` is empty. Still used in internal logic; no longer drives serve-time filtering (replaced by `user_ids` attribution). |
| `_needs_processing(video_id, feed_dir)` | Returns `True` if the video has no `metadata.json` in the archive (new video or recovery path). Videos with any `metadata.json` are considered done and are not reprocessed. |
| `_collect_channel_focuses(channel_id, feed_focus)` | Reads `state/users/*/user.json` and returns a list of `(focus, user_ids)` pairs for *channel_id*. Feed-level *feed_focus* comes first with `user_ids=[]` (unrestricted). User focuses are read from `user["channels"][channel_id]`; if multiple users share a focus their IDs are merged into one entry. Returns `[("", [])]` if nothing is configured (single unrestricted call). Capped at `MAX_FOCUSES_PER_CHANNEL` (10). |

### YouTube data-gathering

| Function | Description |
|---|---|
| `_is_youtube_short(video_id)` | Returns `True` if the video is a YouTube Short by following the watch URL redirect and checking for `/shorts/` in the final URL. Fails open (returns `False`) on any network error so a transient check failure never skips a real video. |
| `discover_videos(channel_id)` | Fetches YouTube's official Atom RSS feed (`feeds/videos.xml?channel_id=…`); returns `list[VideoInfo]` (up to 15 most-recent, exact ISO dates). Retries up to 3 times on network errors. |
| `fetch_transcript(video_id, supadata_client, ..., transcript_rate_limit_event=None)` | Fetches timed transcript segments from Supadata; returns formatted string or None. When a quota-exhausted error is detected (HTTP 402 or `SupadataError` with a credit-related `error` code), sets `transcript_rate_limit_event` before returning None so callers can stop immediately. When Supadata raises an exception whose message contains `"live streaming"`, returns `None` (transient) so the video is retried next run without being permanently marked. |

### AI story generation

| Function | Description |
|---|---|
| `call_gemini_api(...)` | Posts to Gemini REST API; returns `list[GeminiStory]`, `False` on rate-limit, `None` on failure |
| `write_story_files(stories, meeting_dir, video_id="", *, clear_existing=True, start_index=1)` | Writes each `GeminiStory` as a numbered `.md` file; includes `**Topics:**` when topics are present and `**Users:**` when the story dict has a non-empty `_user_ids` key (set by `process_video` before calling this function). Use `clear_existing=False, start_index=N` to append new stories from additional focus passes without removing existing files. |

### RSS feed builders

**Naming convention:** `build_*` functions return content (bytes or HTML) and never touch disk — the web app calls these directly to serve feeds dynamically. `rebuild_*` functions write to disk and are called by the CLI/scraper to produce static files. `rebuild_user_feed` is a thin wrapper: it calls `build_user_feed_xml` and writes the result to disk.

| Function | Description |
|---|---|
| `rebuild_feed(feed_dir, feed_cfg)` | Generates `content/<channel>/rss.xml` (all stories) |
| `rebuild_aggregate_feed(base_url)` | Generates `content/rss.xml` from all channels (all stories) |
| `build_user_feed_xml(user, base_url)` | Builds and returns RSS feed XML bytes for a user's subscribed channels; does **not** write to disk |
| `rebuild_user_feed(user, base_url)` | Thin CLI wrapper: calls `build_user_feed_xml` and writes `state/users/<slug>/rss.xml` to disk |
| `rebuild_user_feed_page(user, base_url)` | Generates `state/users/<slug>/index.html` — a self-contained feed page with stories from subscribed channels |

### Processing orchestration

| Function | Description |
|---|---|
| `process_video(video_id, ..., focuses=None, transcript_rate_limit_event=None)` | Fetch + analyse one video. *focuses* is a list of `(focus_string, user_ids)` pairs; calls Gemini once per pair and writes `**Users:**` metadata for each story. Falls back to `[(feed["focus"], [])]` (unrestricted) when omitted. Deduplicates stories by title across focus passes, merging user_ids. Returns `("content_written", n)`, `("ai_rate_limited", 0)`, `("transcript_quota_exhausted", 0)`, or `("skipped", 0)`. Skips the transcript API call immediately if `transcript_rate_limit_event` is already set. |
| `process_feed(feed, ..., ai_rate_limit_event=None, transcript_rate_limit_event=None)` | Collects focuses via `_collect_channel_focuses`, processes all videos needing work for any focus; returns `(content_changed, ai_rate_limited, stories_written)`. Breaks out of the video loop immediately when `transcript_rate_limit_event` is set. |
| `_read_channels(config)` | Reads channel list from `state/channels.json`; falls back to `config.get("feeds", [])` when `channels.json` does not exist (migration compatibility). |
| `_check_supadata_quota(config)` | Reads `state/supadata_balance.json` (written at the end of the previous run) and returns `(ok, balance)`. If `ok` is False, `_main_body` records `transcript_quota_exhausted: True` in the run log and exits without processing any videos. No live API call is made — uses only the cached file. |
| `main()` | Entry point: loads config, calls `process_feed` for each configured channel |

---

## Setup

### Prerequisites
- Python 3.10+
- A [Supadata](https://supadata.ai) account and API key
- A [Google AI Studio](https://aistudio.google.com) Gemini API key

### Install Dependencies

```bash
# Production
pip install -r requirements.txt

# Development (adds pytest)
pip install -r requirements-dev.txt
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
  "base_url": ""
}
```

Channel configuration lives in `state/channels.json` (see `channels.json.sample`). Channels are managed via the web UI admin panel or by editing `state/channels.json` directly.

---

## Running

```bash
# Normal run
python3 TubeNews.py

# Debug mode (verbose logging, shows API calls and raw responses)
python3 TubeNews.py --debug

# WebSub daemon mode (runs indefinitely; receives YouTube push notifications)
python3 TubeNews.py --daemon

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
content/
├── <channel_slug>/             # slugified channel_name
│   ├── 2026-03-14_dQw4w9WgXcQ/
│   │   ├── transcript.txt      # Raw Supadata output (SECONDS --> TEXT)
│   │   ├── metadata.json       # {video_id, video_title, status, processed_at}
│   │   ├── 01_Story_Title.md
│   │   └── 02_Another_Story.md
│   ├── 2000-01-01_XXXXXXXXXXX/ # ignored_too_old stubs use 2000 date prefix
│   │   └── metadata.json       # {status: "ignored_too_old"}
│   └── rss.xml                 # Per-channel RSS feed
└── rss.xml                     # Regional aggregate feed (all channels)

state/
├── channels.json               # Channel configuration (replaces feeds[] in TubeNews.json)
├── subscriptions.json          # WebSub subscription tracking (keyed by channel_id)
├── .tubenews.lock              # Process lock file
├── supadata_balance.json       # Cached Supadata credit balance (written at end of each run)
├── run_logs/                   # Run data (replaces content/_run_logs/)
│   ├── run_log.json            # Rolling summary of last 30 runs (written by TubeNews.py)
│   └── run-<pid>.log           # Full stdout/stderr for a single run (written by admin_run_now)
├── users/                      # User account data (replaces content/_users/)
│   ├── index.json              # email→UUID index for O(1) login lookup
│   └── <uuid>/
│       ├── user.json           # Account data (see schema in Web Application section)
│       ├── rss.xml             # Pre-built personal feed (CLI only; web app generates dynamically)
│       └── index.html          # Pre-built feed page (CLI only; web app generates dynamically)
└── queue/
    └── push_queue.json         # WebSub incoming video queue ({video_id, channel_id, queued_at})
```

All content scanners (`rebuild_aggregate_feed`, `build_user_feed_xml`, `_archive_channel_stats`, etc.) operate only on `content/` and ignore `state/`. The `serve_content` route serves only files under `content/`; `state/` is never web-accessible.

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

`status` values: `"processed"` | `"ignored_too_old"` | `"ignored_short"` (video is a YouTube Short; will not be retried) | `"no_stories"` (AI ran but returned no relevant stories) | `"no_transcript_available"` (Supadata confirmed no captions exist; will not be retried)

`processed_focuses` is a sorted list of all focus strings for which Gemini has been called on this video. Old `metadata.json` files that pre-date this field have no `processed_focuses` key and are treated as fully processed (not re-run).

---

## Configuration Reference

| Key | Required | Description |
|---|---|---|
| `gemini_api_key` | Yes | Google Gemini API key from AI Studio |
| `gemini_model` | Yes | Gemini model name (e.g. `gemini-2.5-flash`) |
| `supadata_api_key` | Yes | Supadata API key for transcript fetching |
| `content_dir` | No | Path to the content directory (default: `content/` next to `TubeNews.py`). Use an absolute path (e.g. `/var/www/html/tubenews`) or a path relative to `TubeNews.py` to point it at your web server's document root. |
| `state_dir` | No | Path to the state directory (default: `state/` next to `TubeNews.py`). Stores users, run logs, channel config, lock file, and Supadata balance — never web-served. Use an absolute path or a path relative to `TubeNews.py`. |
| `request_timeout` | No | Seconds before giving up on YouTube RSS and Supadata API calls (default: `15`). Increase on slow or high-latency connections |
| `gemini_call_delay` | No | Seconds to sleep between consecutive Gemini API calls within a single video's focus passes (default: `5`). Keeps call rate well under the 15 RPM free-tier limit. Set to `0` to disable. |
| `base_url` | No | Public URL of `content/rss.xml`, used as the aggregate feed self-link |
| `ntfy_topic` | No | ntfy.sh topic for run-summary push notifications (e.g. `"TubeNewsAdmin"`); omit to disable |
| `max_parallel_feeds` | No | Max channels processed concurrently (default: `1`; capped at number of feeds). Keep at `1` unless you have a paid Gemini tier with higher RPM limits. |
| `port` | No | Port the Flask web UI listens on (default: `8000`) |
| `tubenews_key` | Web UI only | Flask session secret key — generate with `python -c 'import secrets; print(secrets.token_hex(32))'`; also readable from `TUBENEWS_SECRET_KEY` env var |
| `admin_users` | No | List of email addresses granted admin access to the web UI (e.g. `["alice@example.com"]`) |
| `websub_callback_url` | No | Public HTTPS URL TubeNews registers with YouTube's hub as the push endpoint (e.g. `https://yourdomain.com/youtube/push`). Required for `--daemon` mode. |
| `websub_secret` | No | HMAC-SHA1 signing secret for verifying push payloads from the hub. Generate with `python3 -c 'import secrets; print(secrets.token_hex(32))'`. |
| `websub_daemon_port` | No | Port the WebSub HTTP receiver listens on (default: `8675`). Must be accessible from the internet or a reverse proxy. |
| `websub_min_age_minutes` | No | Minimum age (minutes) a queued push notification must reach before the processor acts on it (default: `360`). Avoids processing livestreams before they end. |
| `websub_check_interval_minutes` | No | How often (minutes) the processor thread wakes to check for pending push notifications (default: `10`). |

---

## Helper Scripts

| Script | Purpose | When to Use |
|---|---|---|
| `helpers/catchup.py` | Marks all visible videos as ignored | Before first run on a channel with existing videos |
| `helpers/check_quota.py` | Tests Gemini API key quota across models | When AI calls fail with 429 errors |
| `helpers/dump_channel_html.py` | Dumps raw `ytInitialData` JSON for a channel tab (legacy diagnostic tool — no longer used by the main pipeline, which now uses the RSS feed) | Kept for historical reference; not needed for normal operation |
| `helpers/reset_password.py` | Resets a user's password from the CLI | When the admin is locked out and can't log in to use the admin panel |

---

## Running Tests

```bash
pytest tests/ -v
```

| File | Covers |
|---|---|
| `tests/test_tubenews.py` | `slugify`, `parse_story_file` (including `topics`, `user_ids`, and HTML escaping in `body_html`), `_story_matches_focus`, `write_story_files` (including `**Users:**` output), `_collect_channel_focuses` (tuple return type, user-id merging), `discover_videos` (RSS parse, HTTP error, malformed XML, network retry, empty feed warning, no `is_live` field), `fetch_transcript` (success path, language mismatch, live-stream skip, quota exhaustion + event flag), the JSON extraction regex in `call_gemini_api`, `rebuild_feed`, `rebuild_aggregate_feed`, `build_user_feed_xml`, lock/unlock helpers, config resolution |
| `tests/test_web.py` | `web/app.py` URL helpers (`_rss_url`, `_feed_url`), user preferences |
| `tests/test_webapp.py` | Flask routes: login guards, rate-limit (429 after 10 attempts), locked-account rejection, dashboard subscription save, admin guards, public token routes, lock-file detection, run-now trigger, channel browse YouTube link, admin runs channel links |

All tests use `tmp_path` fixtures — no network calls and no real archive needed. For functions that hit external APIs (`fetch_transcript`, `call_gemini_api`), use `monkeypatch` or `unittest.mock.patch`.

---

## Development Notes

### External Dependencies and Fragility

- **YouTube RSS feed** (`discover_videos`) fetches `https://www.youtube.com/feeds/videos.xml?channel_id=CHANNEL_ID` — YouTube's official, stable Atom feed. It returns up to 15 most-recent videos with exact ISO 8601 publication dates. No API key, no HTML parsing, no bot-detection concerns. If video discovery stops working entirely, verify the channel ID is correct and the feed URL returns valid XML. The `helpers/dump_channel_html.py` script is kept as a legacy diagnostic tool but is no longer part of the normal workflow.
- **Live stream handling:** The RSS feed includes in-progress livestreams with no indication they are live. When `fetch_transcript` is called for such a video, Supadata raises an exception whose message contains `"live streaming"`; `fetch_transcript` catches this and returns `None` (transient failure), so `process_video` skips the video without writing `metadata.json` and it is retried on the next run. The cost is one extra Supadata API call per live video per run until the stream ends.
- **RSS video limit:** The YouTube Atom feed returns at most 15 entries. This is sufficient for incremental processing (new videos since the last run). `helpers/catchup.py` uses the same RSS feed, so on a first-time setup it marks the 15 most-recent videos as already processed; for channels with a deeper backlog, older stubs must be created manually or the operator must run `catchup.py` before the 16th new video is posted.
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
subscriptions, and a dashboard for sharing feeds. It imports
`build_user_feed_xml`, `parse_story_file`, `slugify`,
and `STORAGE_ROOT` directly from `TubeNews.py`.

### User Storage

Each user is stored as a UUID-named directory under `state/users/`:

```
state/users/
└── <uuid>/
    ├── user.json      # account data (see schema below)
    ├── rss.xml        # personal RSS feed (pre-built by CLI; not used by web app)
    └── index.html     # personal feed page (built by rebuild_user_feed_page; not used by web app)
```

**`user.json` schema:**

```json
{
  "name": "Alice",
  "email": "alice@example.com",
  "password_hash": "scrypt:...",
  "channels": {
    "UCxxxxxxx": ["housing, zoning, permits", "transportation, roads, transit"],
    "UCyyyyyyy": []
  },
  "feed_token": "550e8400-e29b-41d4-a716-446655440000",
  "created_at": 1741910400,
  "last_accessed": 1741910400,
  "locked": false,
  "feed_name": "Alice's Local News",
  "preferences": {"dark_mode": false, "font_size": "normal"},
  "seen_channel_ids": ["UCxxxxxxx", "UCyyyyyyy"],
  "read_articles": ["abc123hash", "def456hash"],
  "starred_articles": ["abc123hash"],
  "bundles": [{"name": "City Government", "channel_ids": ["UCxxxxxxx", "UCyyyyyyy"]}]
}
```

- `channels` — merged subscription + focus dict. Keys are subscribed channel IDs; values are **lists of focus strings** (one per focus line, up to 3). An empty list means no filter (show all stories from that channel). Every subscribed channel must appear as a key, even with an empty focus list. `_collect_channel_focuses` reads this at processing time to determine which Gemini calls to make for each channel.
- `last_accessed` — Unix timestamp (float) of the user's most recent authenticated page view. Updated by the `inject_body_classes` context processor with a 5-minute debounce (at most one disk write per 5 minutes per user). Key absent on accounts created before this field was added.
- `feed_token` — UUID generated at registration; authenticates all public (no-login) URLs for that user. Rotating it invalidates both the RSS feed URL and the feed page URL simultaneously.
- `locked` — boolean; when `true` the account fails `is_active` and is rejected by flask-login on every request without needing to log out. Admin-toggled via `/admin/user/<uid>/lock`.
- `feed_name` — optional custom title shown on the user's `/feed` page (e.g. `"Alice's Local News"`). Key absent means the default `"<name>'s TubeNews"` title is used.
- `preferences` — display settings dict with keys `dark_mode` (bool) and `font_size` (`"normal"` | `"large"` | `"larger"`). Converted to CSS classes by `_prefs_to_classes()` and applied to `<html>` via the `inject_body_classes` context processor. Key absent means all defaults (light mode, normal font).
- `seen_channel_ids` — list of channel IDs the user has "seen" on the dashboard. The `inject_body_classes` context processor diffs this against the current feed list to compute `unseen_channel_count`, which drives the red badge on the "Settings" nav link. Key absent means not yet initialised (pre-feature users); treated as 0 unseen so existing users aren't badged on upgrade. Written (covering all current channels) whenever the user loads or saves the dashboard.
- `read_articles` — sorted list of `content_hash` strings for articles the user has marked as read. `/feed` (Unread tab) hides stories whose hash is in this list; `/read` (Read tab) shows only those stories. Key absent means no articles have been read. Written by the `account_mark_read`, `account_mark_unread`, `account_mark_all_read`, and `account_mark_all_unread` routes. Growth is bounded (~117 KB/year at 10 stories/day × 32 bytes/hash) and individual unread is preserved.
- `starred_articles` — sorted list of `content_hash` strings for articles the user has starred. `/starred` shows only these stories. Key absent means no starred articles. Written by the `account_mark_starred` and `account_mark_unstarred` routes. Independent of read/unread state.
- `bundles` — list of `{name: str, channel_ids: [str]}` dicts defining user-created channel bundles. Bundles appear in the sidebar between "All Channels" and the individual channel list. Clicking a bundle filters the view to stories from those channels via `?bundle=<slug>`. The slug is `slugify(name).lower()`, computed at runtime. Key absent means no bundles. Written by `POST /account/bundles`.

### Token Model

One `feed_token` per user covers two public URLs:

| URL | Content |
|---|---|
| `/feed/<token>.xml` | Personal RSS feed |
| `/feed/<token>.html` | Personal feed page |

Both `/feed/<token>.xml` and `/feed/<token>.html` are generated **dynamically
on every request** — no static file is read or written by the web app.
The extension-less variant `/feed/<token>` also works for backwards
compatibility (serves the RSS feed).

### On-request Generation Flow

Both the feed and feed page are generated fresh on each request from the live archive:

**RSS feed** (`/feed/<token>.xml`):
1. Token matched to a user in `state/users/`
2. `build_user_feed_xml()` scans `content/` and builds feedgen XML in memory
3. XML bytes returned directly as the HTTP response — nothing written to disk

**Feed page** (`/feed/<token>.html` and logged-in `/feed`):
1. `_get_user_stories()` scans `content/` and returns all stories from the user's
   subscribed channels
2. Flask renders `feed.html` with the story list and returns HTML

Because both read the content directory on every request, they always reflect the
latest stories without any explicit rebuild step.

`rebuild_user_feed()` and `rebuild_user_feed_page()` exist in `TubeNews.py` as
standalone utilities (used by the CLI, or for generating static snapshots) but
the web app does **not** call either — the web UI uses dynamic generation only.

### Key Helpers

| Function | Description |
|---|---|
| `_load_config()` | Reads `TubeNews.json`; returns `{}` on failure |
| `_save_channels(channels)` | Atomically writes the channel list to `state/channels.json` (write-then-rename) |
| `_load_channels()` | Returns the channel list from `state/channels.json`; falls back to `feeds[]` in `TubeNews.json` when `channels.json` does not exist (migration compatibility) |
| `_base_url()` | Returns `base_url` from config (empty string if not set) |
| `_rss_url(token)` | Builds the full `/feed/<token>.xml` URL using `base_url` or `url_for` |
| `_feed_url(token)` | Builds the full `/feed/<token>.html` URL using `base_url` or `url_for` |
| `_find_archive_dir_for_channel(channel_id)` | Scans `content/*/channel.json` and returns the `Path` whose `channel_id` matches; used by `admin_feed_edit` to locate the channel dir regardless of historical directory naming |
| `_read_email_index()` | Returns the email→UUID dict from `state/users/index.json`; returns `{}` on any error |
| `_write_email_index(index)` | Atomically writes the email→UUID dict to `state/users/index.json` (write-then-rename) |
| `_index_add(email, uid)` | Adds or updates an entry in the email index |
| `_index_remove(email)` | Removes an entry from the email index |
| `_find_user_by_email(email)` | O(1) lookup via `index.json`; falls back to a glob scan and repairs the index if the entry is missing or stale |
| `_find_user_by_id(uid)` | Loads a user by their UUID directory name |
| `_all_users()` | Returns all users sorted by name |
| `_web_ntfy(title, message, priority)` | Sends a best-effort ntfy.sh notification from web events (registration, manual run trigger). No-op when `ntfy_topic` is not configured; exceptions are silently swallowed so a notification failure never breaks a request. |
| `_is_running()` | Returns `True` if a TubeNews.py process currently holds the lock file (PID is live). Used by `admin_runs` to show a "currently running" indicator and by `admin_run_log` to decide when to stop auto-refreshing. |
| `_sanitize_focus(text)` | Strips characters outside `[\w\s,\-]`, collapses whitespace, and truncates to 100 characters. Applied to every focus line before saving to `user.json`. Security-relevant: prevents prompt injection via the focus field. |
| `_prefs_to_classes(prefs)` | Converts a user preferences dict (`dark_mode`, `font_size`) to a CSS class string applied to `<html>` by the base template. |
| `_safe_next(url)` | Returns `url` only when it is a safe same-site relative path (starts with `/`, not `//`). Used by the login route to prevent open-redirect attacks after authentication. |
| `_channel_info_for_dir(channel_dir)` | Reads `channel.json` from a content directory and returns `ChannelInfo`. Returns `None` if the file is absent or unparseable. Used by all archive scanners. |
| `_archive_channel_stats()` | Scans all channel directories and returns `list[ChannelStat]` with per-channel counts of processed, ignored, no-stories, and story files. Used by `admin_feeds`. |
| `_get_channel_stories(channel_id)` | Returns `(channel_name | None, list[StoryDict])` for a single channel. Used by `channel_feed`. |
| `_get_user_stories(user_data, user_id)` | Scans all subscribed channel directories and returns `list[StoryDict]` filtered by the user's `user_ids` attribution. Used by `serve_feed`, `serve_read`, `serve_all`, and the public feed route. |
| `_channel_counts(stories)` | Takes a `list[StoryDict]` and returns `list[dict]` with `channel_id`, `channel_name`, and `count` keys, sorted by count descending. Used by all four feed routes to populate the channel sidebar. |
| `_user_bundles(user_data)` | Returns the user's `bundles` list with a `slug` field added to each entry (`slugify(name).lower()`). Returns `[]` when `bundles` key is absent. |
| `_bundle_counts(stories, bundles)` | Annotates each bundle dict with a `count` of matching stories from *stories*. Used by all four feed routes to populate bundle sidebar entries. |
| `_get_supadata_balance()` | Reads the cached Supadata credit data from `state/supadata_balance.json`; returns `None` if absent. Used by `admin_feeds` to show the credit balance without a live API call. |

### Route Map

**No authentication required:**

| Method | Route | Handler | Description |
|---|---|---|---|
| GET | `/` | `index` | Redirects to dashboard or login |
| GET/POST | `/login` | `login` | Login form (rate-limited: 10/min) |
| GET/POST | `/register` | `register` | Registration form (rate-limited: 5/min) |
| GET | `/content/<path>` | `serve_content` | Serves files from `content/` directory; blocks any path starting with `_` (covers `_users/`, `_run_logs/`, etc.) |
| GET | `/feed/<token>.xml` | `serve_rss` | Personal RSS feed by token |
| GET | `/feed/<token>.html` | `serve_feed_public` | Personal feed page by token |
| GET | `/transcript/<channel_slug>/<meeting_id>` | `serve_transcript` | Renders a transcript as an HTML page with per-segment anchors; URL fragment `#t<seconds>` scrolls to and highlights that segment. Path-traversal guarded. No auth required — transcripts are public content. |

**Login required:**

| Method | Route | Handler | Description |
|---|---|---|---|
| GET/POST | `/dashboard` | `dashboard` | Subscribe to channels; shows feed URLs |
| POST | `/logout` | `logout` | Clears session (POST + CSRF to prevent logout CSRF) |
| GET | `/feed` | `serve_feed` | Serves the logged-in user's unread (inbox) stories; accepts `?channel=<channel_id>` to filter to a single channel |
| GET | `/starred` | `serve_starred` | Serves the logged-in user's starred stories; accepts `?channel=<channel_id>` to filter to a single channel |
| GET | `/read` | `serve_read` | Serves the logged-in user's read stories (the "Read" tab); accepts `?channel=<channel_id>` to filter to a single channel |
| GET | `/all` | `serve_all` | Serves all of the logged-in user's stories regardless of read status; accepts `?channel=<channel_id>` to filter to a single channel (applied after any `?q=` search) |
| GET | `/channel/<channel_id>` | `channel_feed` | Browse all stories for one channel (no time cutoff); passes `channel_id` to `feed.html` so the sub-header can link to the YouTube channel page |
| GET/POST | `/account` | `account` | Self-service account settings: change name/email (requires current password) |
| POST | `/account/password` | `account_password` | Change own password (requires current password; new password min 10 chars) |
| POST | `/account/rotate-token` | `account_rotate_token` | Issue a new feed token; invalidates old RSS/feed URLs |
| POST | `/account/delete` | `account_delete` | Delete own account (requires current password + email confirmation) |
| POST | `/account/mark-read` | `account_mark_read` | Add a `content_hash` to the user's `read_articles`; returns JSON `{"ok": true}` |
| POST | `/account/mark-unread` | `account_mark_unread` | Remove a `content_hash` from `read_articles`; returns JSON `{"ok": true}` |
| POST | `/account/mark-all-read` | `account_mark_all_read` | Mark all current stories as read; redirects to `/feed` |
| POST | `/account/mark-all-unread` | `account_mark_all_unread` | Clear all read articles (mark everything unread); redirects to `/feed` |
| POST | `/account/bundles` | `account_bundles` | Save user-defined channel bundles (list of `{name, channel_ids}` parsed from indexed form fields); key absent or empty name = delete bundle |
| POST | `/account/mark-starred` | `account_mark_starred` | Add a `content_hash` to the user's `starred_articles`; returns JSON `{"ok": true}` |
| POST | `/account/mark-unstarred` | `account_mark_unstarred` | Remove a `content_hash` from `starred_articles`; returns JSON `{"ok": true}` |

**Admin required (`admin_users` in config):**

| Method | Route | Handler | Description |
|---|---|---|---|
| GET | `/admin` | `admin_users` | User list |
| POST | `/admin/users/add` | `admin_user_add` | Create a new user account (name, email, password); validates input and uniqueness |
| GET | `/admin/user/<uid>` | `admin_user` | User detail / edit |
| POST | `/admin/user/<uid>/info` | `admin_user_info` | Update name and email |
| POST | `/admin/user/<uid>/subscriptions` | `admin_user_subscriptions` | Update channel subscriptions |
| POST | `/admin/user/<uid>/password` | `admin_user_password` | Reset password |
| POST | `/admin/user/<uid>/lock` | `admin_user_lock` | Toggle account lock |
| POST | `/admin/user/<uid>/prefs` | `admin_user_prefs` | Update display preferences (font size, dark mode) for the target user |
| POST | `/admin/user/<uid>/promote` | `admin_user_promote` | Toggle admin status for the target user (adds/removes email from `admin_users` in `TubeNews.json`); self-promotion is blocked |
| POST | `/admin/user/<uid>/rotate-token` | `admin_rotate_token` | Issue new feed token (invalidates old URLs) |
| POST | `/admin/user/<uid>/delete` | `admin_user_delete` | Delete account (requires email confirmation) |
| GET | `/admin/feeds` | `admin_feeds` | Feed (channel) list |
| GET/POST | `/admin/feeds/add` | `admin_feed_add` | Add a channel to config; calls `_wsb_subscribe` after saving when WebSub is configured |
| GET/POST | `/admin/feeds/<channel_id>/edit` | `admin_feed_edit` | Edit a channel in config; renames the archive directory when `channel_name` changes so the back catalog is preserved |
| POST | `/admin/feeds/<idx>/delete` | `admin_feed_delete` | Remove a channel from config; calls `_wsb_unsubscribe` after saving when WebSub is configured |
| GET | `/admin/runs` | `admin_runs` | Run history; shows per-run log links and a "View log" link for the currently-running process |
| POST | `/admin/run-now` | `admin_run_now` | Launch a manual TubeNews.py run; stdout/stderr redirected to `state/run_logs/run-<pid>.log` |
| GET | `/admin/run-log/<int:pid>` | `admin_run_log` | Stream the captured log for the run with the given PID; auto-refreshes while that PID holds the lock |
| GET | `/admin/feed` | `admin_all_stories` | Feed view of all stories from all channels — the HTML counterpart to `content/rss.xml`; links to that aggregate feed in the sub-header |
| POST | `/admin/story/delete` | `admin_story_delete` | Delete a single story `.md` file; rebuilds the per-channel and aggregate feeds; only accepts numbered `.md` filenames (path traversal guarded) |

### Sticky Sub-Header Row

`base.html` exposes a `header_sub` Jinja block. When a child template overrides the block with content, a 36 px sticky band (`position: sticky; top: 52px; z-index: 190`) appears immediately below the main navigation header and stays visible while scrolling. When the block is left empty (the default), nothing is rendered and no space is reserved.

**Current uses:**
- `feed.html` always renders a sub-header with the feed/page name, three navigation tabs (Unread / Read / All), optional bulk-action buttons, and an RSS pill link on the right.
  - **Tabs** are shown only for authenticated users not on a channel page. The active tab is highlighted. On channel pages, the tabs appear but All is always active.
  - **Bulk-action buttons** (Mark All Read, Mark All Unread) appear in a centered `hs-center` zone between the tabs and the RSS link. Unread tab shows Mark All Read; Read tab shows Mark All Unread; All tab shows both. Hidden when the story list is empty. Rendered as `<button class="btn-sm">` inside `<form style="display:contents">` to bypass the global `button[type="submit"]` style rule.
  - **RSS link** (`hs-rss` pill) appears on the right for authenticated personal pages and channel pages.
  - When the template receives a `channel_id` variable (set only by `channel_feed()`), a "▶ YouTube channel" link is also added to the right.

**Sizing note:** The sub-header's `top: 52px` assumes the main header is exactly 52 px tall (set by `header { height: 52px }` in `style.css`). The transcript banner uses the same `top: 52px` on a page that has no sub-header, so there is no conflict. If the main header height ever changes, update both `top` values together.

### Security Notes

- All state-changing routes use CSRF tokens (flask-wtf).
- Login and register routes are rate-limited (flask-limiter). Rate limiting uses per-worker in-memory storage; with 4 gunicorn workers the effective limit is 4× the configured value. Upgrading to Redis storage would enforce a true global limit.
- `ProxyFix` middleware (`werkzeug.middleware.proxy_fix.ProxyFix`) is applied with `x_for=1, x_proto=1, x_host=1` so rate limiting and IP logging see real client IPs when running behind nginx/Caddy. Only one proxy level is trusted — do not increase `x_for` unless there are multiple proxy hops.
- `/logout` is POST-only with CSRF protection to prevent logout CSRF attacks.
- `serve_content` blocks any path starting with `_`. This rule was historically needed to guard `_users/` and `_run_logs/`; those directories have moved to `state/` (which is never under `content/` and never web-served), but the guard is kept for defence-in-depth.
- `SESSION_COOKIE_SECURE` is only set when `TUBENEWS_HTTPS=true` is in the
  environment, so local dev works without HTTPS.
- Admins are determined solely by email match against `admin_users` in
  `TubeNews.json` — there is no `is_admin` flag stored in `user.json`.
- Locked accounts (`"locked": true`) fail `is_active` and are rejected by
  flask-login on every request without needing to log out.
- Changing account name or email (the `/account` info action) requires the current password, just like the password-change and delete flows.
- `body_html` in `ParsedStory` contains HTML-escaped text joined with `<br>` tags. HTML special characters are escaped in `parse_story_file()` so the `{{ story.body_html | safe }}` in templates is safe to render.
- User directories are deleted with `shutil.rmtree()` (not a manual file loop) so deletion works even when subdirectories are present.
- `_save_channels()` writes `state/channels.json` atomically via write-then-rename (same pattern as `_write_email_index`). `admin_user_promote()` writes `TubeNews.json` by the same pattern, preventing partial writes from corrupting either file.

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

- `_rss_url(token)` returns `/feed/<token>.xml` and `_feed_url(token)` returns `/feed/<token>.html` when `base_url` is not set in `TubeNews.json`. They only prepend an absolute base when the operator has explicitly configured `base_url`.
- All links rendered in HTML templates must be relative (use `url_for()` without `_external=True`, or hardcoded root-relative paths like `/feed/...`).
- The only place absolute URLs are appropriate is inside generated RSS/Atom XML, and only when `base_url` is configured.

---

## Commit & Branch Conventions

This project uses descriptive commit messages. When working on this repo as an AI assistant, push to the branch specified in the task instructions.
