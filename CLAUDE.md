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
| `config.json` | Runtime config (gitignored; copy from `.sample`) |

---

## Architecture

- **Filesystem as database.** `content/` holds stories; `state/` holds users, queue, logs, lock. No SQL.
- **Incremental.** Any video directory with a `metadata.json` is skipped permanently.
- **Meeting dirs are named by video ID only** — e.g. `content/channel_slug/dQw4w9WgXcQ/`. No date prefix.
- **Per-user attribution.** Stories get a `**Users:**` line listing whose focus triggered them. `_get_user_stories()` filters by UUID at serve time. Stories without `**Users:**` are shown to all subscribers.
- **WebSub daemon (default mode).** YouTube pushes new video notifications; processor thread wakes every ~1 min and works through `state/queue/push_queue.json`. Use `--single-run` for cron-style use.
- **AI backoff.** Gemini errors map to different backoffs: 429 RPM → 2 min, 429 RPD → 12 h, 503 → 5 min. `retry_count` is NOT incremented during backoff (only genuine per-video Gemini failures count).
- **Transcript caching.** `transcript.txt` existence skips Supadata. Delete it to re-fetch.
- **Supadata cost depends on `supadata_transcript_mode`.** In `native` (the default here) a request is ~1 credit, including one that answers "no transcript". In `auto`/`generate` — the SDK's own default is `auto` — it falls back to speech-to-text billed **by video duration**; a multi-hour meeting has cost 1430 credits. Never call `client.transcript()` without passing `mode`. See "Supadata Credit Budget" below.
- **Config hot-reload.** Most `config.json` keys reload each processor cycle. Immutable: `websub_callback_url`, `websub_secret`, `websub_daemon_port`. Reloadable podcast keys: `tts_provider`, `tts_api_key`, `tts_voice_id`, `tts_language_code`, `podcast_generation_hour`, `podcast_retention_days`.

---

## Key Conventions

- **`sanitize_focus()`** is in `tubenews_utils.py`; `_sanitize_focus` in `web/app.py` is an alias. ASCII-only regex — Unicode homoglyphs are stripped intentionally.
- **`url_for()` — never `_external=True`** in web templates or routes. Use `_rss_url(token)` / `_feed_url(token)` helpers which use `base_url` from config when set.
- **Atomic writes.** `_write_email_index` and `_save_channels` use write-then-rename. `_index_add`/`_index_remove` use `fcntl.flock(LOCK_EX)` for multi-worker safety.
- **`call_gemini_api` return values:** `list[GeminiStory]` (success), `False` (429 RPM — short backoff), `"quota_exhausted_daily"` (429 RPD — 12 h backoff), `"service_unavailable"` (503 — 5 min backoff), `None` (transient — retry later).
- **`process_video` return values:** `("content_written", n)`, `("ai_rate_limited", 0)`, `("transcript_quota_exhausted", 0)`, `("skipped", 0)`.
- **`fetch_transcript` return values:** `str` (success), `None` (transient), `False` (permanent — no captions, 403, 404).
- **`metadata.json` `processed_at`** is an ISO 8601 string (`"2026-04-07T00:14:36Z"`), not a float.
- **Web app generates feeds dynamically** — `build_user_feed_xml()` and `_get_user_stories()` scan `content/` on every request. It returns `(stories, has_more)` and takes `offset`/`limit`/`lookback_days`; pass `limit=None` for a full scan. `rebuild_user_feed*` functions are CLI-only.
- **A `metadata.json` with any status is final.** `_needs_processing()` returns False for it — including `no_transcript_available`. Retry scheduling belongs to the queue, never to a second age-based window layered on top.
- **Admin fetch() routes answer JSON.** `/admin/feeds/<id>/toggle` and `/delete` return JSON when the request sends `X-Requested-With: XMLHttpRequest`, and redirect otherwise. Never fetch a redirecting route with `redirect: 'manual'` — the response comes back opaque with `status === 0`, which reads as a failure even when the change succeeded, leaving the UI stale.
- **Registration abuse controls.** `/register` has a honeypot field (`website`, hidden via CSS not `display:none`), a tightened rate limit (5/hour/IP), and the "new user" ntfy notification fires on **verification** (`verify_email()`), not on the raw `POST /register` — anyone can hit that route with an address they don't own. Unverified accounts older than `_UNVERIFIED_USER_MAX_AGE_HOURS` (72h) are swept up by `_cleanup_stale_unverified_users()` once a day. Admin UI has a bulk-delete for clearing out an existing spam wave (`/admin/users/bulk-delete`, checkboxes + "Select unverified").

---

## Supadata Credit Budget

Supadata bills **one credit per transcript request** — *but only in `native` mode*. This is the single most important thing to know about this integration.

**`supadata_transcript_mode` (default `native`) is the difference between 1 credit and 1400.** The SDK's own default is `"auto"`, which silently falls back to speech-to-text when a video has no captions, and generation is **billed by video duration, not per request**. Two multi-hour council meetings cost 1430 credits each against a 300/month plan. Always pass `mode` explicitly; never rely on the SDK default.

Because of this, the request caps below are only a budget in `native` mode — they count *requests*, and a request is only ~1 credit when generation is off. If you ever set `auto`/`generate`, these caps stop bounding spend. A response carrying a `job_id` (HTTP 202) means a generation job was billed; `fetch_transcript` treats that as permanent and never retries, since each retry starts another paid job.

The plan is small (e.g. 300/month), so an unbounded retry loop can also drain a month in a day.

Bounds, outermost last:

| Bound | Constant | Applies to |
|---|---|---|
| Caption retries | `_NO_CAPTIONS_MAX_ATTEMPTS` (5) | Supadata says the video has no captions — a definitive answer, so a short leash (~T+4h) |
| Transient retries | `_TRANSCRIPT_MAX_ATTEMPTS` (len of `_TRANSCRIPT_RETRY_OFFSETS`) | Network/service failures only — no answer received, worth waiting out |
| Livestream re-queues | `_LIVESTREAM_MAX_ATTEMPTS` (12) | Stream still broadcasting. Counted in `livestream_attempts`, which survives the `transcript_attempts` reset in `_requeue_video` |
| Stale backlog | `max_video_age_days` (config, default 14) | Costs **no** credit — decided from the queue entry's date. A queue that built up while the daemon was down would otherwise spend the whole fresh allowance on stale videos before reaching today's uploads. Written off as `ignored_too_old`. `0` disables. |
| **Hard daily cap** | `supadata_daily_limit` (config, default 10) | *Every* call. Bounds the blast radius of a runaway loop to one day's worth. |
| **Hard cycle cap** | `supadata_monthly_limit` (config, default 300) | *Every* call. The number the vendor actually bills against. |

`fetch_transcript` reserves a credit via `_supadata_budget_reserve()` before each request; when either cap is spent it logs *which one*, sets the quota event, and returns `None`. Counters live in `state/supadata_usage.json` (`date`/`count` for the day, `cycle_start`/`cycle_count` for the billing cycle).

**When Supadata itself refuses a request** (no credits, or rate limited), the reserved credit is refunded — the vendor served nothing and does not bill a refusal — and a persisted backoff (`backoff_until` in the same file) stops further calls for `_SUPADATA_QUOTA_BACKOFF_SECONDS` (6 h) or `_SUPADATA_RATE_BACKOFF_SECONDS` (5 min). Without that backoff the processor retried every cycle, and because the credit was reserved *before* the call, a single unfetchable video walked the daily counter to its cap and blocked every other video for the rest of the day. The backoff applies even when metering is disabled: it is about the vendor refusing, not about the local budget.

A per-minute rate limit (`rate` in the error code, or HTTP 429) is **not** an exhausted plan — both once matched the same broad `"limit"` keyword. Vendor error codes are now logged verbatim, so the two can be told apart from the logs.

**A daily cap alone cannot honour a monthly plan** — 10/day over a 31-day cycle is 310 requests against a 300-credit plan. That is why the cycle bound exists. Vendors bill from the signup date, not the 1st, so set `supadata_billing_cycle_day` to match the plan's reset day.

The hard caps are the backstop: they hold even if a future retry path is added without a bound of its own. Tests must not meter against them — `tests/conftest.py` disables metering suite-wide.

Current usage appears in the daemon heartbeat log every 5 minutes, and the effective mode plus both caps are logged once at startup (`_log_supadata_cost_posture`) — a non-native mode logs a WARNING, because the caps count requests and cannot bound spend when generation is on.

### Rule for any paid API

**Pass every cost-relevant argument explicitly; never inherit an SDK default.**
A default you did not choose is a spending decision made for you. `tests/test_tubenews.py`
enforces this for Supadata: `test_supadata_sdk_has_no_unreviewed_parameters` fails if
`Supadata.transcript()` gains a parameter this code neither passes nor has vetted, so a
new knob cannot reach production without someone checking what it costs. Apply the same
pattern to any future metered dependency.

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
