# TubeNews — Maintainability TODO

---

## Deferred: Deploy Script Flexibility

`deploy.sh` is currently hardcoded to one Bastille jail path. Future improvements:
- Accept jail name as argument (e.g., `./deploy.sh TubeNews`)
- Auto-detect Bastille jails or ask operator to choose
- Support non-Bastille FreeBSD installations
- Query operator for paths if they don't match defaults

Currently acceptable since the script is specific to one operator's setup.

---

## Potential Future Improvement: Parallelization

Parallel channel processing is implemented via `ThreadPoolExecutor` in
`_main_body`, capped by `max_parallel_feeds` (default 3). All thread-safety
issues identified when this section was written have since been resolved
(see Completed Items). One further opportunity remains:

**Parallel YouTube tab scraping (`discover_videos`)**
The `videos` and `streams` tabs are fetched sequentially inside
`discover_videos`. Fetching them concurrently with a two-worker
`ThreadPoolExecutor` would roughly halve per-channel discovery time with zero
shared mutable state — no race risk. Low priority given that transcript and
Gemini calls dominate wall-clock time.

**Videos within a single feed cannot be parallelized without deeper redesign.**
`ai_rate_limited` and `transcript_rate_limit_event` are shared events threaded
serially through `process_feed`. Parallelizing intra-feed video processing
would require restructuring that state propagation.

---

## Design Decisions & Far-Future Considerations

### Storage architecture

~~The current storage model is intentionally simple:

- **Feeds** are stored as a JSON array in `config.json` under `feeds`.
  This is the operator config file — read directly by the CLI tool at startup.
- **Users** are stored as individual `content/_users/<uuid>/user.json` files.
  Discovery happens by globbing `content/_users/*/user.json` at runtime.

These two patterns are deliberately different: feeds are configuration (small,
operator-managed, read at startup), users are application state (runtime-created,
individually owned).

**User data should eventually move outside `content/`.**  The current location
(`content/_users/`) is a half-step: the `_` prefix makes the directory
invisible to all content scanners and blocked by `serve_content` with a single
rule, but user account data (password hashes, email addresses, tokens) still
lives inside the same tree as public RSS and story content.  The right long-term
fix is to move user storage to a sibling directory (e.g. a top-level `users/`
or `data/users/` next to `content/`) so the separation is structural, not just
naming convention.  This requires updating `USERS_ROOT` in `web/app.py`,
`STORAGE_ROOT / "_users"` in `TubeNews.py`, and the corresponding paths in all
tests, plus a one-time migration script for existing installs.

**`_run_logs/` can also move outside `content/`** once user data moves out.
All three references to it are in `web/app.py` only (`admin_runs`,
`admin_run_now`, `admin_run_log`) — `TubeNews.py` does not write there — so
moving `_run_logs/` to a sibling directory only requires updating those three
references together. The admin Runs page will continue to work as long as all
three are updated in the same change.~~ **Done — see Completed Items (state directory restructure and channels.json migration).**

~~**If the user count grows large enough that glob-scanning on every login/lookup
becomes a bottleneck**, consider adding a lightweight `content/_users/index.json`
that maps email → uuid. The per-user files stay as-is; the index just speeds up
`_find_user_by_email()`. Rebuild the index on registration, deletion, and email
change. No schema migration needed for existing user directories.~~ **Done — see Completed Items.**

~~**If `config.json` becomes unwieldy** (many feeds + many server config keys),
consider splitting it:

- `config.json` — server/runtime config only: API keys, model name, base URL,
  port, admin emails, rate limits, etc.
- `feeds.json` — the channel list only.

Both files would live in the project root. `TubeNews.py` and `web/app.py` would
need small updates to load from two files. This separation makes it easier to
check `feeds.json` into version control (no secrets) while keeping
`config.json` gitignored.~~ **Done — see Completed Items (channels.json migration).**

---

## Completed Items

### Parallel channel processing with all race conditions resolved (March 2026)

`_main_body` now runs channels concurrently via `ThreadPoolExecutor`, capped
by the `max_parallel_feeds` config key (default 3). All races identified in
the pre-parallelization analysis were addressed:

- **`content/rss.xml` aggregate feed:** `rebuild_aggregate_feed()` is called
  exactly once after `executor.map()` returns (all threads joined), using a
  `any_content_changed` `threading.Event` as the flag. It never runs inside a
  thread.
- **`ai_rate_limited` / `transcript_rate_limit_event`:** Both are
  `threading.Event` objects shared across threads; `.set()` is idempotent and
  the check-then-skip pattern is safe.
- **Duplicate `channel_id` TOCTOU:** `_main_body` validates the feeds list for
  duplicate `channel_id` values before spawning any threads and exits with a
  clear error if found, preventing two threads from processing the same channel
  directories concurrently.
- **Concurrency cap:** `max_parallel_feeds` (default 3) limits simultaneous
  outbound API calls.

### Email index for O(1) user lookup (March 2026)

`_find_user_by_email()` previously globbed `content/_users/*/user.json` on every
login, duplicate-email check, and admin info update — O(n) in the number of
users. A `content/_users/index.json` file (email → UUID dict) now provides O(1)
lookup. The index is written atomically (write-then-rename) and is kept in sync
on registration, admin-created accounts, account deletion, and email changes.
`_find_user_by_email()` still falls back to a glob scan if the index is missing
or an entry is stale, and repairs the index on the fly — so existing deployments
upgrade without any manual migration step.

### Per-user per-channel focus filtering (March 2026)

Users can now set a personal focus per channel subscription (e.g. "housing,
zoning") stored in `channel_focus` in their `user.json`.  Gemini tags each
story with a `topics` list; `_story_matches_focus()` filters stories at serve
time in both `_get_user_stories()` and `build_user_feed_xml()`.  Old stories
(written before topic tagging was added) always pass through unfiltered.
No API cost increase — one Gemini call per video regardless of subscriber count.

### Silent `except Exception:` blocks now log skips (March 2026)

All bare `except Exception: continue` / `pass` blocks in file-scanning loops
across `TubeNews.py` and `web/app.py` now capture the exception as `exc` and
emit `logger.debug(f"Skipping {path}: {exc}")`.  One-off failures (config load,
run-log load) use `logger.warning(...)` instead.  Intentionally silent fallbacks
(graceful degradation on missing config keys, ntfy notification failures) were
left unchanged.

### Admin feed-management routes and focus filtering now tested (March 2026)

`tests/test_webapp.py` now covers `/admin/feeds` (list, add, delete) including
auth guards, validation errors (missing fields, bad channel ID prefix,
duplicate channel ID), and the happy path for each mutation.  Two new tests
exercise `_get_user_stories()` focus filtering via the `/feed` Flask route
directly: one asserts that a user with focus "housing" sees only housing
stories; the other confirms all stories appear when no focus is configured.
261 tests pass.

### State directory restructure (April 2026)

Internal state moved out of `content/` into a sibling `state/` directory.
`_run_logs/` → `state/run_logs/`, `_users/` → `state/users/`, `.tubenews.lock`
→ `state/.tubenews.lock`, Supadata balance cache → `state/supadata_balance.json`.
`resolve_roots(config_file, base_dir)` added to `tubenews_utils.py` to resolve
both roots from `content_dir` / `state_dir` config keys. All scanners, route
handlers, and tests updated. Existing installs migrate automatically: the web UI
and scraper create `state/` on first run; no data in `content/` needs to move
(story files remain in `content/`).

### channels.json migration (April 2026)

Channel configuration moved from `feeds[]` in `config.json` to
`state/channels.json`. `_save_channels()` in `web/app.py` writes atomically to
`state/channels.json`. `_read_channels(config)` in `TubeNews.py` and
`_load_channels()` in `web/app.py` both read from `state/channels.json` with
fallback to `config["feeds"]` for migration compatibility. `channels.json.sample`
added to the repo. The `feeds[]` key in `config.json` is no longer written by
the application but continues to be read as a fallback until operators migrate.

### WebSub `--daemon` mode (April 2026)

YouTube PubSubHubbub push support added via `python3 TubeNews.py --daemon`. Two
threads: `_wsb_receiver_thread` (HTTP server on `websub_daemon_port`) receives
and verifies signed push payloads; `_wsb_processor_thread` wakes every
`websub_check_interval_minutes` and dispatches queued notifications older than
`websub_min_age_minutes`. Push queue stored in `state/queue/push_queue.json`;
subscription state in `state/subscriptions.json`. Subscribe/unsubscribe called
automatically from `admin_feed_add` / `admin_feed_delete` in the web UI. Renewal
is self-managed: re-subscribe all on daemon startup; re-subscribe expiring
subscriptions (within 24 h) on each processor cycle. Lease = 604 800 s (7 days);
hub = `https://pubsubhubbub.appspot.com/subscribe`.

### Windows datetime portability fixed (March 2026)

`%-d` and `%-I` strftime format codes replaced with a new `_fmt_no_leading_zeros(dt, fmt)`
helper that uses `%d`/`%I` and strips leading zeros via `re.sub(r" 0(\d)", r" \1", ...)`.
Works identically on Windows, Linux, and macOS.

### `helpers/catchup.py` `slugify()` duplication eliminated (March 2026)

`slugify()` extracted into `tubenews_utils.py` (no heavy dependencies).
`TubeNews.py` now imports from it (`from tubenews_utils import slugify`) and
re-exports it so all existing callers (`web/app.py`, tests) are unaffected.
`helpers/catchup.py` adds `sys.path.insert(0, str(BASE_DIR))` and imports
from `tubenews_utils` directly, removing its local copy.

### TypedDict data contracts introduced (March 2026)

All bare `dict` and `list[dict]` type annotations on public function signatures
have been replaced with named `TypedDict` classes.  Defined in `TubeNews.py`:
`VideoInfo`, `FeedConfig`, `GeminiStory`, `ParsedStory`, `MetadataDict`,
`FeedResult`.  Defined in `web/app.py`: `ChannelInfo`, `ChannelStat`,
`StoryDict`.  `FeedConfig` and `ParsedStory` are imported into `web/app.py`
from `TubeNews`.  See the "Data Contracts" section in `CLAUDE.md` for the full
field listing.

---

## Known Issues / Future Hardening

The following issues were identified in a QA sweep and deferred because they
are low-risk in current usage or require larger refactoring to address cleanly.

### Web UI — login/register rate-limiting behaviour untested

Login and register routes are rate-limited (flask-limiter, 10/min and 5/min
respectively).  Testing this requires firing many real requests to trip the
limiter and verifying the 429 response; the test suite disables rate-limiting
globally via `RATELIMIT_ENABLED = False`, so no simple unit test covers this
path.  Acceptance-level or load tests would be the right vehicle.
