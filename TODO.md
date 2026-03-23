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

**If the user count grows large enough that glob-scanning on every login/lookup
becomes a bottleneck**, consider adding a lightweight `archive/users/index.json`
that maps email → uuid. The per-user files stay as-is; the index just speeds up
`_find_user_by_email()`. Rebuild the index on registration, deletion, and email
change. No schema migration needed for existing user directories.

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

### Per-user per-channel focus filtering (March 2026)

Users can now set a personal focus per channel subscription (e.g. "housing,
zoning") stored in `channel_focus` in their `user.json`.  Gemini tags each
story with a `topics` list; `_story_matches_focus()` filters stories at serve
time in both `_get_user_stories()` and `build_user_feed_xml()`.  Old stories
(written before topic tagging was added) always pass through unfiltered.
No API cost increase — one Gemini call per video regardless of subscriber count.

---

## Known Issues / Future Hardening

The following issues were identified in a QA sweep and deferred because they
are low-risk in current usage or require larger refactoring to address cleanly.

### Bare `except Exception:` blocks hide errors

Several inner loops in `rebuild_aggregate_feed`, `rebuild_user_feed`, and
`rebuild_user_blog` use `except Exception: continue` to skip corrupt story or
metadata files.  The skip behaviour is correct, but the absence of logging
makes it impossible to know which files were skipped or why.

**Future fix:** Add `logger.debug(f"Skipping {path}: {exc}")` in each of these
handlers so silent skips are at least visible in debug mode.

### Type hints use generic `dict` throughout

Functions like `discover_videos()` return `list[dict]` but the actual structure
is `list[{id, title, date, is_live}]`.  Similarly, `process_feed()`,
`process_video()`, and the feed-config dicts are all typed as bare `dict`.

**Future fix:** Define `TypedDict` classes (`VideoInfo`, `FeedConfig`,
`StoryDict`, etc.) and use them in all annotations.  This makes the data
contracts explicit and enables static type checking with mypy/pyright.

### `helpers/catchup.py` duplicates `slugify()`

The helper defines its own `slugify()` to avoid importing TubeNews (which
drags in feedgen, supadata, etc.).  If the slugify implementation ever changes
in `TubeNews.py`, the helper must be updated manually.

**Future fix:** Extract `slugify()` into a tiny `tubenews_utils.py` with no
heavy dependencies, importable by both.

### Windows datetime portability

`%-d` and `%-I` strftime format codes (used in `_send_ntfy()` and `main()`)
are POSIX-only and crash on Windows with a stray `%` error.

**Future fix:** Replace `%-d`/`%-I` with a portable helper that strips leading
zeros after formatting (e.g. `dt.strftime("%B %d, %Y").replace(" 0", " ")`).

### Web UI has no automated tests

~~All Flask routes, the `User` model, and helper functions in `web/app.py` are
currently untested.~~  A `tests/test_web.py` and `tests/test_webapp.py` now
cover URL generation, subscription saves, admin guards, public token routes,
and lock-file detection.  Good baseline coverage exists.

**Remaining gaps:**
- Login/register rate-limiting behaviour is not exercised by the test suite.
- The admin feed-management routes (`/admin/feeds/*`) are untested.
- `_get_user_stories()` focus filtering is covered indirectly through
  `build_user_feed_xml` tests but has no dedicated integration-level tests
  against the Flask routes.
