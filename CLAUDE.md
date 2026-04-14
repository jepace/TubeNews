# TubeNews — Developer Quick Reference

> Full reference (function signatures, TypedDicts, route map, schema details): **`DEVREF.md`**

---

## What It Is

Daemon that turns YouTube channels into per-user AI-written news feeds.
Pipeline: YouTube RSS → Supadata transcripts → Gemini stories (`.md`) → RSS + web UI.

---

## Critical Files

| File | Role |
|---|---|
| `TubeNews.py` | Everything: discovery, transcripts, AI, feeds, WebSub daemon |
| `web/app.py` | Flask UI: accounts, subscriptions, dashboard, admin |
| `tubenews_utils.py` | `slugify`, `resolve_roots`, `sanitize_focus` — shared between main + helpers |
| `web/templates/` | Jinja2 templates; `base.html` → `feed.html`, `account.html`, etc. |
| `web/static/style.css` | All CSS; CSS vars for dark/light mode |
| `state/channels.json` | Channel list (managed via admin UI or directly; replaces `feeds[]` in JSON) |
| `state/users/<uuid>/user.json` | Per-user account, prefs, subscriptions, digest + podcast state |
| `TubeNews.json` | Runtime config (gitignored; copy from `.sample`) |

---

## Architecture

- **Filesystem as database.** `content/` holds stories; `state/` holds users, queue, logs, lock. No SQL.
- **Incremental.** Any video directory with a `metadata.json` is skipped permanently.
- **Meeting dirs are named by video ID only** — e.g. `content/channel_slug/dQw4w9WgXcQ/`. No date prefix.
- **Per-user attribution.** Stories get a `**Users:**` line listing whose focus triggered them. `_get_user_stories()` filters by UUID at serve time. Stories without `**Users:**` are shown to all subscribers.
- **WebSub daemon (default mode).** YouTube pushes new video notifications; processor thread wakes every ~1 min and works through `state/queue/push_queue.json`. Use `--single-run` for cron-style use.
- **AI backoff.** Gemini 429 or 503 → `False` return from `call_gemini_api` → 1-hour backoff. `retry_count` is NOT incremented during backoff (only genuine per-video Gemini failures count).
- **Transcript caching.** `transcript.txt` existence skips Supadata. Delete it to re-fetch.
- **Config hot-reload.** Most `TubeNews.json` keys reload each processor cycle. Immutable: `websub_callback_url`, `websub_secret`, `websub_daemon_port`. Reloadable podcast keys: `tts_provider`, `tts_api_key`, `tts_voice_id`, `tts_language_code`, `podcast_generation_hour`, `podcast_retention_days`.

---

## Key Conventions

- **`sanitize_focus()`** is in `tubenews_utils.py`; `_sanitize_focus` in `web/app.py` is an alias. ASCII-only regex — Unicode homoglyphs are stripped intentionally.
- **`url_for()` — never `_external=True`** in web templates or routes. Use `_rss_url(token)` / `_feed_url(token)` helpers which use `base_url` from config when set.
- **Atomic writes.** `_write_email_index` and `_save_channels` use write-then-rename. `_index_add`/`_index_remove` use `fcntl.flock(LOCK_EX)` for multi-worker safety.
- **`call_gemini_api` return values:** `list[GeminiStory]` (success), `False` (429/503 — disable AI), `None` (transient — retry later).
- **`process_video` return values:** `("content_written", n)`, `("ai_rate_limited", 0)`, `("transcript_quota_exhausted", 0)`, `("skipped", 0)`.
- **`fetch_transcript` return values:** `str` (success), `None` (transient), `False` (permanent — no captions, 403, 404).
- **`metadata.json` `processed_at`** is an ISO 8601 string (`"2026-04-07T00:14:36Z"`), not a float.
- **Web app generates feeds dynamically** — `build_user_feed_xml()` and `_get_user_stories()` scan `content/` on every request. `rebuild_user_feed*` functions are CLI-only.

---

## Storage Layout (brief)

```
content/<channel_slug>/<video_id>/   ← stories, transcript.txt, metadata.json
content/<channel_slug>/rss.xml
content/rss.xml                      ← aggregate feed

state/channels.json
state/queue/push_queue.json
state/users/index.json               ← email→UUID index
state/users/<uuid>/user.json
state/users/<uuid>/podcast/          ← MP3 episodes + JSON sidecars
state/users/<uuid>/podcast.xml       ← per-user iTunes podcast RSS
state/run_logs/
```

---

## Testing Policy

- **Tests ship with the code.** New/modified functions need tests in the same commit.
- **Full suite must pass before push.** `pytest tests/ -v` — all green.
- **Bug fixes need a regression test.**

```bash
pytest tests/ -v
python3 -m pylint TubeNews.py web/app.py --max-line-length=120
python3 -m mypy TubeNews.py web/app.py --ignore-missing-imports
```

---

## Documentation Policy

Update in the same commit: `DEVREF.md` (function/schema details), `README.md` (user-facing), `SERVING.md` (ops), `TODO.md` (completed items).

---

## Commit & Branch

Push to the branch specified in session instructions. Descriptive commit messages.
