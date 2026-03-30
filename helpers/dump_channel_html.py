#!/usr/bin/env python3
"""Dump the raw ``ytInitialData`` JSON from a channel tab page.

Use this when the YouTube scraper in ``TubeNews.py`` stops discovering
videos or stops extracting titles and dates correctly.  YouTube embeds
a ``ytInitialData`` JSON blob in every channel page; the TubeNews scraper
parses that blob to find video IDs, titles, and upload dates.  When YouTube
changes the blob structure, this script lets you inspect the new layout so
you can update ``_parse_channel_page_metadata()`` accordingly.

Usage::

    python3 helpers/dump_channel_html.py [channel_id [tab]]

``tab`` is ``videos`` (default) or ``streams``.  If ``channel_id`` is
omitted, reads the first channel from ``TubeNews.json``.  When the warning
log tells you which channel and tab triggered the error, copy-paste the
suggested command directly — it now includes both.

Writes two files:

* ``/tmp/yt_data_<tab>.json`` — the full ``ytInitialData`` blob (pretty-printed).
  Open this in a text editor or ``jq`` to explore the structure.
* ``/tmp/yt_raw.html`` — the raw page HTML, written only if the blob is not
  found (e.g. YouTube served a bot-detection interstitial instead).

The script also prints a quick summary of the first video renderer it finds
so you can immediately see which JSON keys are present without opening the
full dump.
"""

import json
import re
import sys
from pathlib import Path

import requests

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "Chrome/122.0.0.0 Safari/537.36"
    )
}

CONFIG_FILE = Path(__file__).resolve().parent.parent / "TubeNews.json"


def find_video_renderers(obj: object, found: list | None = None) -> list[dict]:
    """Recursively collect every dict that has both ``videoId`` and ``title`` keys.

    This mirrors the shape of a ``videoRenderer`` object in ``ytInitialData``.
    The function walks the entire JSON tree so it works regardless of where
    YouTube nests the renderers.

    Args:
        obj:   Any JSON-decoded value (dict, list, or scalar).
        found: Accumulator list; pass ``None`` on the initial call.

    Returns:
        List of dicts that contain at least ``videoId`` and ``title``.
    """
    if found is None:
        found = []
    if isinstance(obj, dict):
        if "videoId" in obj and "title" in obj:
            found.append(obj)
        for v in obj.values():
            find_video_renderers(v, found)
    elif isinstance(obj, list):
        for item in obj:
            find_video_renderers(item, found)
    return found


def main() -> None:
    if len(sys.argv) > 1:
        channel_id = sys.argv[1]
        channel_name = channel_id  # no friendly name available from CLI
    else:
        try:
            config = json.loads(CONFIG_FILE.read_text())
        except FileNotFoundError:
            sys.exit(f"Error: {CONFIG_FILE} not found — copy TubeNews.json.sample first.")
        except json.JSONDecodeError as exc:
            sys.exit(f"Error: could not parse {CONFIG_FILE}: {exc}")

        feeds = config.get("feeds", [])
        if not feeds:
            sys.exit("No feeds configured in TubeNews.json — nothing to fetch.")

        channel_id = feeds[0]["channel_id"]
        channel_name = feeds[0]["channel_name"]

    tab = sys.argv[2] if len(sys.argv) > 2 else "videos"
    if tab not in {"videos", "streams"}:
        sys.exit(f"Error: tab must be 'videos' or 'streams', got {tab!r}.")

    print(f"Channel : {channel_name}  ({channel_id})")
    print(f"Tab     : {tab}")

    url = f"https://www.youtube.com/channel/{channel_id}/{tab}"
    print(f"Fetching {url} …")
    try:
        r = requests.get(url, headers=HEADERS, timeout=15)
        r.raise_for_status()
    except Exception as exc:
        sys.exit(f"Request failed: {exc}")

    print(f"HTTP {r.status_code}  ({len(r.text):,} bytes)")

    # Extract the ytInitialData JSON blob embedded in the page.
    m = re.search(
        r"var ytInitialData\s*=\s*(\{.*?\});\s*(?:var |</script>)",
        r.text,
        re.DOTALL,
    )
    if not m:
        raw_out = Path("/tmp/yt_raw.html")
        raw_out.write_text(r.text)
        print(
            "ytInitialData NOT FOUND in page.\n"
            "YouTube may be serving a bot-detection interstitial.\n"
            f"Raw HTML saved to {raw_out} for inspection."
        )
        sys.exit(1)

    try:
        data = json.loads(m.group(1))
    except json.JSONDecodeError as exc:
        sys.exit(f"Found ytInitialData but could not parse it as JSON: {exc}")

    out = Path(f"/tmp/yt_data_{tab}.json")
    out.write_text(json.dumps(data, indent=2))
    print(f"ytInitialData written to {out}  ({out.stat().st_size:,} bytes)")

    # Print a quick preview of the first video renderer so you can see which
    # keys are present without opening the full dump.
    renderers = find_video_renderers(data)
    print(f"\nFound {len(renderers)} videoRenderer-like objects.")
    if renderers:
        first = renderers[0]
        print("First renderer top-level keys:", list(first.keys()))
        for key in (
            "videoId",
            "title",
            "publishedTimeText",
            "dateText",
            "badges",
            "thumbnailOverlays",
        ):
            if key in first:
                print(f"  {key}: {json.dumps(first[key])[:200]}")
    else:
        print(
            "No renderers found — the JSON structure may have changed.\n"
            f"Open {out} and search for 'videoId' to find the new location."
        )


if __name__ == "__main__":
    main()
