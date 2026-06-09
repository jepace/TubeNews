#!/usr/bin/env python3
"""Mark all currently-visible videos as already processed.

Run this ONCE before the first ``TubeNews.py`` run on any channel that
already has published videos.  Without it, TubeNews will try to fetch
transcripts and generate stories for every video it can see — potentially
dozens of old meetings that aren't relevant any more.

How it works
------------
For each configured channel, this script fetches YouTube's official Atom
RSS feed and writes a minimal ``metadata.json`` stub
(``status: "ignored_too_old"``) into a new archive directory for every
video ID found.  TubeNews treats any directory that already contains a
``metadata.json`` as done and skips it, so only videos published *after*
this script runs will be picked up by TubeNews.

Note: YouTube's RSS feed returns the 15 most recent videos.  If a channel
has more than 15 existing videos and you need them all marked, run this
script as soon as possible after adding the channel — before the 16th new
video is posted — or create stubs manually for any older video IDs you need
to suppress.

Usage::

    python3 helpers/catchup.py

Safe to re-run: videos that already have an archive directory are skipped.
"""

import json
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import requests

# ---------------------------------------------------------------------------
# Resolve paths the same way TubeNews.py does so content_dir/state_dir
# settings in config.json are respected.
# ---------------------------------------------------------------------------

BASE_DIR = Path(__file__).resolve().parent.parent
CONFIG_FILE = BASE_DIR / "config.json"

sys.path.insert(0, str(BASE_DIR))
from tubenews_utils import resolve_roots, slugify  # noqa: E402

STORAGE_ROOT, STATE_ROOT = resolve_roots(CONFIG_FILE, BASE_DIR)

_YT_RSS_NS = {
    "atom": "http://www.w3.org/2005/Atom",
    "yt":   "http://www.youtube.com/xml/schemas/2015",
}


# ---------------------------------------------------------------------------
# Core logic
# ---------------------------------------------------------------------------


def _read_channels(config: dict) -> list[dict]:
    """Return configured channels from state/channels.json or config.json feeds[]."""
    channels_file = STATE_ROOT / "channels.json"
    if channels_file.exists():
        try:
            return json.loads(channels_file.read_text())
        except Exception:
            pass
    return config.get("feeds", [])


def catchup() -> None:
    """Iterate every configured channel and stub out all visible video IDs.

    Fetches YouTube's official Atom RSS feed for each channel (returns up to
    15 most-recent videos) and creates a ``2000-01-01_<id>/metadata.json``
    stub with ``status: "ignored_too_old"`` for each video found.  TubeNews
    will skip any video whose archive directory already contains a
    ``metadata.json``.

    Videos that already have an archive directory (from a previous run or a
    prior TubeNews session) are left untouched.
    """
    try:
        config = json.loads(CONFIG_FILE.read_text())
    except FileNotFoundError:
        sys.exit(f"Error: {CONFIG_FILE} not found — copy config.json.sample first.")
    except json.JSONDecodeError as exc:
        sys.exit(f"Error: could not parse {CONFIG_FILE}: {exc}")

    feeds = _read_channels(config)
    if not feeds:
        sys.exit("No channels configured in state/channels.json — nothing to do.")

    for feed in feeds:
        channel_name = feed["channel_name"]
        channel_id = feed["channel_id"]
        chan_slug = slugify(channel_name)
        print(f"[*] {channel_name}")
        feed_dir = STORAGE_ROOT / chan_slug
        feed_dir.mkdir(parents=True, exist_ok=True)

        # Fetch video IDs from the official YouTube Atom RSS feed.
        found_ids: list[str] = []
        url = f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"
        try:
            r = requests.get(url, timeout=15)
            r.raise_for_status()
            root = ET.fromstring(r.text)
            for entry in root.findall("atom:entry", _YT_RSS_NS):
                vid_el = entry.find("yt:videoId", _YT_RSS_NS)
                if vid_el is not None and vid_el.text:
                    found_ids.append(vid_el.text.strip())
        except Exception as exc:
            print(f"    [!] Warning: could not fetch RSS feed: {exc}")

        print(f"    Found {len(found_ids)} video IDs in RSS feed.")

        new_count = 0
        for v_id in found_ids:
            # Skip if any directory for this video already exists.
            if any(d.name == v_id for d in feed_dir.iterdir() if d.is_dir()):
                continue

            # Write a stub marking this video as ignored from the catchup period.
            stub_dir = feed_dir / v_id
            stub_dir.mkdir(exist_ok=True)
            meta = {
                "video_id": v_id,
                "video_title": "Backlog Catchup",
                "video_date": "2000-01-01",
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
