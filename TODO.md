# TubeNews — Maintainability TODO

Issues discovered during code review, grouped by priority. Tackle Critical items first as they affect correctness; High items affect reliability and security; Medium and Low items improve long-term maintainability.

---

## Critical — Bugs

- [x] **`rebuild_feed()`: `html_body` undefined (line 152, `TubeNews.py`)** *(fixed)*
  Removed dead conditional; `fe.content()` now uses `body_text` directly.

- [x] **`helpers/check_quota.py`: hardcoded API key** *(fixed)*
  Now reads `gemini_api_key` from `TubeNews.json` via config file.

- [x] **`helpers/catchup.py`: wrong `CONFIG_FILE` path** *(fixed)*
  `BASE_DIR` corrected to `Path(__file__).resolve().parent.parent` so it resolves to the project root.

---

## High — Reliability & Security

- [x] **Hardcoded API keys scrubbed from git history** *(fixed)*
  Google Gemini key and Supadata key removed from all commits using `git filter-repo`. Both keys were burnt and should be rotated in Google AI Studio and the Supadata dashboard.

- [x] **FreeBSD-only SSL cert path breaks other platforms (`TubeNews.py` line 13)** *(fixed)*
  Now guarded with `os.path.exists()` — no-op on Linux/macOS.

- [x] **`AI_DISABLED` global mutable flag (`TubeNews.py` line 22)** *(fixed)*
  Global removed. `generate_news()` now returns `False` on rate-limit (vs `None` for other failures). `main()` tracks `ai_disabled` as a local variable.

- [x] **Bare `except: pass` in `helpers/catchup.py` (line 29)** *(fixed)*
  Now catches `Exception as e` and prints a warning message.

- [x] **`output_dir` config key is unused** *(fixed)*
  Removed from `TubeNews.json.sample` to eliminate the misleading entry.

---

## Medium — Maintainability

- [x] **`slugify()` duplicated across files** *(acknowledged — won't extract)*
  Extracting to `helpers/utils.py` requires awkward `sys.path` manipulation since `helpers/` is not a Python package. The function is a single line that never changes; duplication is acceptable.

- [x] **No formal test suite** *(fixed)*
  Added `tests/test_tubenews.py` with 19 pytest tests covering `slugify`, the JSON story extraction regex, `rebuild_feed`, and `rebuild_meta_feed`. Run with: `python3 -m pytest tests/ -v`

- [x] **YouTube scraping fails silently when HTML structure changes** *(fixed)*
  `discover_video_ids()` now logs a `WARNING` when a 200 response yields 0 video IDs.

- [x] **Transcript truncation is silent** *(fixed)*
  `generate_news()` now logs a `WARNING` with character count when a transcript exceeds 100,000 chars.

- [x] **RSS meta-feed link hardcoded to localhost** *(fixed)*
  `rebuild_meta_feed(base_url="")` now accepts an optional URL. Add `"base_url": "https://..."` to `TubeNews.json` to set the RSS self-link. Defaults to `youtube.com` alternate link when omitted.

---

## Low — Code Quality

- [x] **Missing docstrings on several functions** *(fixed)*
  Docstrings added to `slugify`, `discover_video_ids`, `rebuild_feed`, `rebuild_meta_feed`, and `main`.

- [x] **One-liner chaining hurts readability** *(fixed)*
  All `;`-chained statements split onto separate lines in `rebuild_feed`, `rebuild_meta_feed`, and `main`.

- [x] **`rebuild_meta_feed()` reads each story file 3 times** *(fixed)*
  File is now read once into `raw_text` and reused for splitlines, MD5 hash, and timestamp regex.

- [x] **`rebuild_feed()` inner loop `break` logic is awkward** *(fixed)*
  Guard check moved to the top of each loop iteration (`if count >= 50: break`) eliminating the trailing duplicate breaks.

- [x] **`get_transcript_and_meta()`: fragile attribute access** *(fixed)*
  The Supadata `Transcript` object has no `metadata`, `title`, or date fields. Title and upload date are now scraped from the YouTube video page HTML (`"uploadDate"` JSON-LD field, `<title>` tag) — zero extra API cost.
