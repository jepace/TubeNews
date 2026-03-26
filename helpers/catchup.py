#!/usr/bin/env python3
"""Mark all currently-visible videos as already processed.

Run this ONCE before the first ``TubeNews.py`` run on any channel that
already has published videos.  Without it, TubeNews will try to fetch
transcripts and generate stories for every video it can see — potentially
dozens of old meetings that aren't relevant any more.

How it works
------------
For each configured channel, this script scrapes the ``/videos`` and
``/streams`` tabs on YouTube and writes a minimal ``metadata.json`` stub
(``status: "ignored_too_old"``) into a new archive directory for every
video ID found.  TubeNews treats any directory that already contains a
``metadata.json`` as done and skips it, so only videos published *after*
this script runs will be picked up by the main scraper.

Usage::

    python3 helpers/catchup.py

Safe to re-run: videos that already have an archive directory are skipped.
"""

import json
import re
import sys
from pathlib import Path

import requests

# ---------------------------------------------------------------------------
# Resolve paths the same way TubeNews.py does so archive_dir is respected.
# ---------------------------------------------------------------------------

BASE_DIR = Path(__file__).resolve().parent.parent
CONFIG_FILE = BASE_DIR / "TubeNews.json"

try:
    _cfg = json.loads(CONFIG_FILE.read_text())
    # "content_dir" is the current key; "archive_dir" is accepted for existing installs.
    _content_dir = _cfg.get("content_dir") or _cfg.get("archive_dir", "")
    if _content_dir:
        _p = Path(_content_dir)
        STORAGE_ROOT = _p if _p.is_absolute() else (BASE_DIR / _p).resolve()
    else:
        STORAGE_ROOT = BASE_DIR / "content"
except Exception:
    STORAGE_ROOT = BASE_DIR / "content"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "Chrome/122.0.0.0 Safari/537.36"
    )
}

sys.path.insert(0, str(BASE_DIR))
from tubenews_utils import slugify  # noqa: E402


# ---------------------------------------------------------------------------
# Core logic
# ---------------------------------------------------------------------------


def catchup() -> None:
    """Iterate every configured channel and stub out all visible video IDs.

    For each video ID found on the channel's ``/videos`` and ``/streams``
    tabs, a ``2000-01-01_<id>/metadata.json`` stub is created with
    ``status: "ignored_too_old"``.  TubeNews will skip any video whose
    archive directory already contains a ``metadata.json``.

    Videos that already have an archive directory (from a previous run or a
    prior TubeNews session) are left untouched.
    """
    try:
        config = json.loads(CONFIG_FILE.read_text())
    except FileNotFoundError:
        sys.exit(f"Error: {CONFIG_FILE} not found — copy TubeNews.json.sample first.")
    except json.JSONDecodeError as exc:
        sys.exit(f"Error: could not parse {CONFIG_FILE}: {exc}")

    feeds = config.get("feeds", [])
    if not feeds:
        sys.exit("No feeds configured in TubeNews.json — nothing to do.")

    for feed in feeds:
        channel_name = feed["channel_name"]
        channel_id = feed["channel_id"]
        chan_slug = slugify(channel_name)
        print(f"[*] {channel_name}")
        feed_dir = STORAGE_ROOT / chan_slug
        feed_dir.mkdir(parents=True, exist_ok=True)

        # Collect video IDs from both the /videos and /streams tabs.
        found_ids: list[str] = []
        for tab in ("videos", "streams"):
            url = f"https://www.youtube.com/channel/{channel_id}/{tab}"
            try:
                r = requests.get(url, headers=HEADERS, timeout=15)
                r.raise_for_status()
                found_ids.extend(re.findall(r'"videoId":"([^"]{11})"', r.text))
            except Exception as exc:
                print(f"    [!] Warning: could not fetch /{tab} tab: {exc}")

        # Deduplicate while preserving order (dict.fromkeys trick).
        found_ids = list(dict.fromkeys(found_ids))
        print(f"    Found {len(found_ids)} video IDs on YouTube.")

        new_count = 0
        for v_id in found_ids:
            # Skip if any directory for this video already exists.
            if any(d.name.endswith(v_id) for d in feed_dir.iterdir() if d.is_dir()):
                continue

            # Write a stub with a year-2000 date prefix so these directories
            # sort visually apart from real meetings (real ones use the actual
            # publication date, e.g. 2026-03-14_...).
            stub_dir = feed_dir / f"2000-01-01_{v_id}"
            stub_dir.mkdir(exist_ok=True)
            meta = {
                "video_id": v_id,
                "video_title": "Backlog Catchup",
                "status": "ignored_too_old",
                "processed_at": 0,
            }
            (stub_dir / "metadata.json").write_text(json.dumps(meta, indent=2))
            new_count += 1

        skipped = len(found_ids) - new_count
        parts = [f"    Marked {new_count} new video(s) as ignored"]
        if skipped:
            parts.append(f"{skipped} already had an archive entry")
        print(", ".join(parts) + ".")

    print("\n[✓] Done. TubeNews will now only process videos published after this run.")


if __name__ == "__main__":
    catchup()
