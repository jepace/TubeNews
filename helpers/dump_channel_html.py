"""Dump the raw ytInitialData JSON from a channel's /videos page.

Run this on a machine that can reach YouTube:

    python helpers/dump_channel_html.py

Reads the first channel_id from TubeNews.json and writes the full
ytInitialData blob to /tmp/yt_data.json so you can inspect the structure.
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

config_path = Path(__file__).parent.parent / "TubeNews.json"
if not config_path.exists():
    sys.exit("TubeNews.json not found — run from the repo root or copy the sample first.")

config = json.loads(config_path.read_text())
channel_id = config["feeds"][0]["channel_id"]
print(f"Using channel: {config['feeds'][0]['channel_name']}  ({channel_id})")

url = f"https://www.youtube.com/channel/{channel_id}/videos"
print(f"Fetching {url} …")
r = requests.get(url, headers=HEADERS, timeout=15)
print(f"HTTP {r.status_code}  ({len(r.text):,} bytes)")

# Pull out the ytInitialData JSON blob
m = re.search(r"var ytInitialData\s*=\s*(\{.*?\});\s*(?:var |</script>)", r.text, re.DOTALL)
if not m:
    print("ytInitialData NOT FOUND in page — YouTube may be serving a bot-detection page.")
    out = Path("/tmp/yt_raw.html")
    out.write_text(r.text)
    print(f"Raw HTML saved to {out} for inspection.")
    sys.exit(1)

data = json.loads(m.group(1))
out = Path("/tmp/yt_data.json")
out.write_text(json.dumps(data, indent=2))
print(f"ytInitialData written to {out}  ({out.stat().st_size:,} bytes)")

# Quick preview: show first videoRenderer keys so we know what's available
def find_video_renderers(obj, found=None):
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

renderers = find_video_renderers(data)
print(f"\nFound {len(renderers)} videoRenderer-like objects with both 'videoId' and 'title'.")
if renderers:
    first = renderers[0]
    print("\nFirst renderer top-level keys:", list(first.keys()))
    for key in ("videoId", "title", "publishedTimeText", "dateText", "badges", "thumbnailOverlays"):
        if key in first:
            print(f"  {key}: {json.dumps(first[key])[:200]}")
