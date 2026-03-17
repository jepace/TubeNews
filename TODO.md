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

**`archive/rss.xml` — the meta-feed (line ~809)**
`rebuild_meta_feed()` is called inside the per-feed loop as soon as a channel
produces content. It reads *all* channel directories, then overwrites the
single shared `archive/rss.xml`. Two threads finishing simultaneously would
both invoke it concurrently, potentially interleaving reads with a partial
write or clobbering each other's output.
Fix: collect a `content_changed` flag per thread, then call
`rebuild_meta_feed()` exactly once after all channel threads have joined.

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
- **`rebuild_meta_feed()`:** Its read-all-channels + write-one-file pattern
  makes it inherently a serial barrier operation; it should always run after
  all other work completes.
