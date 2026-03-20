#!/usr/bin/env python3
import json, requests, re
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
CONFIG_FILE = BASE_DIR / "TubeNews.json"
STORAGE_ROOT = BASE_DIR / "archive"
HEADERS = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/122.0.0.0 Safari/537.36'}

def slugify(text):
    return re.sub(r'[^a-zA-Z0-9]', '_', text).strip('_')

def catchup():
    with open(CONFIG_FILE, 'r') as f:
        config = json.load(f)

    for feed in config['feeds']:
        chan_slug = slugify(feed['channel_name'])
        print(f"[*] Catching up: {feed['channel_name']}")
        feed_dir = STORAGE_ROOT / chan_slug
        feed_dir.mkdir(parents=True, exist_ok=True)

        found_ids = []
        for tab in ["videos", "streams"]:
            url = f"https://www.youtube.com/channel/{feed['channel_id']}/{tab}"
            try:
                r = requests.get(url, headers=HEADERS, timeout=15)
                ids = re.findall(r'"videoId":"([^"]{11})"', r.text)
                found_ids.extend(ids)
            except Exception as e:
                print(f"    [!] Warning: could not fetch tab '{tab}': {e}")

        found_ids = list(dict.fromkeys(found_ids))
        print(f"    Found {len(found_ids)} videos. Marking as ignored...")

        for v_id in found_ids:
            # Check if directory already exists
            if any(d.name.endswith(v_id) for d in feed_dir.iterdir() if d.is_dir()):
                continue

            # Create a stub folder so the main script skips it.
            # Use a 2000-01-01 prefix so stubs sort alongside real meetings
            # in tab completion (both start with '2').
            m_dir = feed_dir / f"2000-01-01_{v_id}"
            m_dir.mkdir(exist_ok=True)
            meta = {
                "video_id": v_id,
                "video_title": "Backlog Catchup",
                "status": "ignored_too_old",
                "processed_at": 0
            }
            (m_dir / "metadata.json").write_text(json.dumps(meta))

    print("\n[✓] Backlog cleared. Main script will now only see TRULY new videos.")

if __name__ == "__main__":
    catchup()
