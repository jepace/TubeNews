# TubeNews — Maintainability TODO

---

## Potential Future Improvement: Parallelization

The script currently runs everything sequentially. Below is an analysis of
where parallelism would help and where it would introduce race conditions.

### High-value opportunities

**1. Parallel YouTube tab scraping (`discover_videos`, lines ~264–279)**
The `videos` and `streams` tabs are fetched one after the other with no
dependency between them. Switching to `ThreadPoolExecutor` with a trivial
merge would halve discovery time per channel. Zero shared mutable state —
no race risk.

**2. Parallel channel processing (`main` loop, lines ~803–810)**
Each channel writes to its own `archive/<slug>/` directory, so the per-channel
pipeline (discovery → transcript fetch → Gemini → per-channel `rss.xml`) is
largely independent. Running channels concurrently is the highest-value target:
most wall-clock time is spent blocked on HTTP responses, and a 5-channel config
would run roughly 5× faster in theory.

### Race conditions to address before parallelizing

**`archive/rss.xml` — the aggregate feed (line ~809)**
`rebuild_aggregate_feed()` is called inside the per-feed loop as soon as a channel
produces content. It reads *all* channel directories, then overwrites the
single shared `archive/rss.xml`. Two threads finishing simultaneously would
both invoke it concurrently, potentially interleaving reads with a partial
write or clobbering each other's output.
Fix: collect a `content_changed` flag per thread, then call
`rebuild_aggregate_feed()` exactly once after all channel threads have joined.

**`ai_disabled` / `ai_rate_limited` flags (lines ~711, ~769, ~800–806)**
These are plain `bool` variables. In a threaded scenario the check-then-act
pattern is not thread-safe. A `threading.Event` (set once when any thread hits
429, checked by all others) would be the right primitive. Practical impact
without the fix: a handful of extra Gemini calls before all threads observe
the flag — wasteful but not data-corrupting.

**`write_story_files()` delete-then-rewrite (lines ~415–416)**
The function globs and unlinks stale `*.md` files, then writes new ones.
If two threads ever processed the *same* video concurrently (e.g. duplicate
channel entries in config, or future intra-feed parallelism), their interleaved
deletes and writes would produce a mixed set of story files. No risk today at
the channel-parallel level because each video's directory name is unique, but
it becomes a TOCTOU hazard if videos within a feed are ever parallelized.

**Shared API keys / quota**
Parallelizing channels multiplies concurrent outbound API calls. The Gemini
rate limit (already a single-point failure) is hit sooner, and Supadata quota
drains faster. Any parallelism implementation should include a concurrency
cap (e.g. `max_workers=2`) or a shared `threading.Semaphore` on Gemini calls.

**`feed_dir.iterdir()` unprocessed check (lines ~723–729)**
The "is this video already archived?" scan reads the directory at a moment in
time. With two threads processing the same channel the scan is a TOCTOU issue:
both see the same unprocessed list and both attempt the same videos. A
per-video lock (keyed on video ID) or an atomic `mkdir` check would fix this.

### What cannot be parallelized without deeper redesign

- **Videos within a single feed:** `ai_rate_limited` is a local variable
  propagated serially through the `process_feed` loop. Parallelizing
  intra-feed videos requires restructuring this state.
- **`rebuild_aggregate_feed()`:** Its read-all-channels + write-one-file pattern
  makes it inherently a serial barrier operation; it should always run after
  all other work completes.

---

## Design Decisions & Far-Future Considerations

### Storage architecture

The current storage model is intentionally simple:

- **Feeds** are stored as a JSON array in `TubeNews.json` under `feeds`.
  This is the operator config file — read directly by the CLI tool at startup.
- **Users** are stored as individual `archive/users/<uuid>/user.json` files
  with no central index. Discovery happens by globbing `archive/users/*/user.json`
  at runtime.

These two patterns are deliberately different: feeds are configuration (small,
operator-managed, read at startup), users are application state (runtime-created,
individually owned).

~~**If the user count grows large enough that glob-scanning on every login/lookup
becomes a bottleneck**, consider adding a lightweight `archive/users/index.json`
that maps email → uuid. The per-user files stay as-is; the index just speeds up
`_find_user_by_email()`. Rebuild the index on registration, deletion, and email
change. No schema migration needed for existing user directories.~~ **Done — see Completed Items.**

**If `TubeNews.json` becomes unwieldy** (many feeds + many server config keys),
consider splitting it:

- `TubeNews.json` — server/runtime config only: API keys, model name, base URL,
  port, admin emails, rate limits, etc.
- `feeds.json` — the channel list only.

Both files would live in the project root. `TubeNews.py` and `web/app.py` would
need small updates to load from two files. This separation makes it easier to
check `feeds.json` into version control (no secrets) while keeping
`TubeNews.json` gitignored.

---

## Completed Items

### Email index for O(1) user lookup (March 2026)

`_find_user_by_email()` previously globbed `archive/users/*/user.json` on every
login, duplicate-email check, and admin info update — O(n) in the number of
users. An `archive/users/index.json` file (email → UUID dict) now provides O(1)
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
exercise `_get_user_stories()` focus filtering via the `/blog` Flask route
directly: one asserts that a user with focus "housing" sees only housing
stories; the other confirms all stories appear when no focus is configured.
261 tests pass.

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
