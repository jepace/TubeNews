import os, json, sqlite3, re, shutil, requests, time
from pathlib import Path

# --- PATHS ---
BASE_DIR = Path(__file__).resolve().parent
CONFIG_FILE = BASE_DIR / "TubeNews.json"
DB_FILE = BASE_DIR / "TubeNews.db"
OLD_TRANSCRIPT_DIR = BASE_DIR / "cache" / "transcripts"
STORAGE_ROOT = BASE_DIR / "archive"
HEADERS = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0.0.0 Safari/537.36'}

def slugify(text):
    return re.sub(r'[^a-zA-Z0-9]', '_', text).strip('_')

def get_channel_id_for_video(video_id):
    """Hits YouTube once to see who owns the video."""
    url = f"https://www.youtube.com/watch?v={video_id}"
    try:
        r = requests.get(url, headers=HEADERS, timeout=10)
        match = re.search(r'"channelId":"(UC[a-zA-Z0-9_-]{22})"', r.text)
        return match.group(1) if match else None
    except: return None

def migrate():
    if not DB_FILE.exists():
        print("No database found. Nothing to migrate.")
        return

    with open(CONFIG_FILE, 'r') as f:
        config = json.load(f)
    
    # Map for easy lookup: { 'UC...': 'Gonzales_Council' }
    channel_map = {f['channel_id']: slugify(f['channel_name']) for f in config['feeds']}
    
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()

    print("[*] Reading processing history from SQLite...")
    cursor.execute("SELECT video_id, added FROM videos")
    processed_videos = cursor.fetchall()

    for v_id, added in processed_videos:
        print(f"[*] Identifying owner of {v_id}...")
        c_id = get_channel_id_for_video(v_id)
        
        if not c_id or c_id not in channel_map:
            print(f"    [!] Skipping {v_id}: Channel not found in your config.")
            continue
            
        channel_slug = channel_map[c_id]
        clean_date = added.split(' ')[0]
        meeting_dir = STORAGE_ROOT / channel_slug / f"{clean_date}_{v_id}"
        meeting_dir.mkdir(parents=True, exist_ok=True)

        # 1. Move Transcript
        old_txt = OLD_TRANSCRIPT_DIR / f"{v_id}.txt"
        if old_txt.exists():
            shutil.copy(old_txt, meeting_dir / "transcript.txt")

        # 2. Create metadata.json
        meta = {
            "video_id": v_id,
            "video_title": "Migrated Video",
            "processed_at": time.time(),
            "channel_id": c_id
        }
        with open(meeting_dir / "metadata.json", "w") as f:
            json.dump(meta, f)
            
        print(f"    [✓] {v_id} moved to {channel_slug}/")

    conn.close()
    print(f"\n[✓] Migration Complete. Data is organized in {STORAGE_ROOT}")

if __name__ == "__main__":
    migrate()
