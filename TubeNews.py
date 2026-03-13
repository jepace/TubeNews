import os, json, sqlite3, requests, re, time, argparse, logging, socket, sys, hashlib
from supadata import Supadata
from feedgen.feed import FeedGenerator
from datetime import datetime, timedelta

# --- CONSTANTS ---
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(BASE_DIR, "TubeNews.json")
DB_FILE = os.path.join(BASE_DIR, "TubeNews.db")
CACHE_DIR = os.path.join(BASE_DIR, "cache")
TRANSCRIPT_DIR = os.path.join(CACHE_DIR, "transcripts")
STORY_DIR = os.path.join(CACHE_DIR, "stories")

# FreeBSD SSL Path Fix
os.environ['SSL_CERT_FILE'] = '/usr/local/share/certs/ca-root-nss.crt'
socket.setdefaulttimeout(20) 
HEADERS = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/122.0.0.0 Safari/537.36'}

logger = logging.getLogger("TubeNews")

def setup_logging(debug_mode):
    level = logging.DEBUG if debug_mode else logging.INFO
    logging.basicConfig(level=level, format='%(asctime)s %(levelname)s: %(message)s', datefmt='%H:%M:%S')

def load_and_validate_config():
    """Load config and handle the inevitable syntax errors gracefully."""
    if not os.path.exists(CONFIG_FILE):
        print(f"CRITICAL: Config file '{CONFIG_FILE}' is missing.")
        sys.exit(1)
    
    try:
        with open(CONFIG_FILE, 'r') as f:
            cfg = json.load(f)
        
        # Audit and Clean: Strip whitespace/newline junk from keys and IDs
        cfg['gemini_api_key'] = str(cfg.get('gemini_api_key', '')).strip()
        cfg['supadata_api_key'] = str(cfg.get('supadata_api_key', '')).strip()
        cfg['ai_model'] = str(cfg.get('ai_model', 'gemini-2.5-flash')).strip()
        
        if not cfg['gemini_api_key'] or not cfg['supadata_api_key']:
            print("CRITICAL: Config is missing API keys.")
            sys.exit(1)
            
        return cfg
    except json.JSONDecodeError as e:
        print(f"--- CONFIG ERROR ---")
        print(f"Your JSON file '{CONFIG_FILE}' has a syntax error at Line {e.lineno}, Col {e.colno}.")
        print(f"Reason: {e.msg}")
        print("Tip: Check for missing double-quotes or trailing commas.")
        sys.exit(1)

def check_environment(output_dir):
    """Ensure we can actually write our results."""
    for d in [TRANSCRIPT_DIR, STORY_DIR]:
        os.makedirs(d, exist_ok=True)
    
    if not os.path.exists(output_dir):
        try:
            os.makedirs(output_dir, exist_ok=True)
        except Exception as e:
            print(f"CRITICAL: Cannot create output directory {output_dir}: {e}")
            sys.exit(1)
            
    if not os.access(output_dir, os.W_OK):
        print(f"CRITICAL: Output directory {output_dir} is not writable by this user.")
        sys.exit(1)

def get_transcript(video_id, sd_client):
    cache_path = os.path.join(TRANSCRIPT_DIR, f"{video_id}.txt")
    if os.path.exists(cache_path):
        logger.debug(f"       [Cache] Hit for {video_id}")
        with open(cache_path, 'r', encoding='utf-8') as f: return f.read()

    logger.info(f"   --> Step 2: Requesting transcript from Supadata...")
    try:
        t_obj = sd_client.transcript(url=f"https://www.youtube.com/watch?v={video_id}", text=False)
        if hasattr(t_obj, 'content') and t_obj.content:
            lines = [f"{int(getattr(s, 'offset', 0)/1000)}s --> {getattr(s, 'text', '')}" for s in t_obj.content]
            text = "\n".join(lines)
            with open(cache_path, 'w', encoding='utf-8') as f: f.write(text)
            return text
    except Exception as e:
        logger.error(f"       [!] Supadata error: {e}")
    return None

def generate_news(transcript, slant, video_title, gemini_key, model_name):
    slant_hash = hashlib.md5((slant + video_title + model_name).encode()).hexdigest()
    cache_path = os.path.join(STORY_DIR, f"{slant_hash}.json")
    if os.path.exists(cache_path):
        logger.debug("       [Cache] Hit for AI analysis.")
        with open(cache_path, 'r', encoding='utf-8') as f: return json.load(f)

    logger.info(f"   --> Step 3: Running AI Analysis ({model_name})...")
    url = f"https://generativelanguage.googleapis.com/v1/models/{model_name}:generateContent?key={gemini_key}"
    
    payload = {"contents": [{"parts": [{"text": f"Slant: {slant}\n\nTranscript: {transcript[:100000]}\n\nReturn a JSON list of objects: [{{'title': '...', 'content': '...', 'start_time_seconds': 123}}]"}]}]}
    
    try:
        res = requests.post(url, json=payload, timeout=90)
        if res.status_code == 429:
            logger.error("       [!] AI Quota Exceeded. Skipping for this run.")
            return None
        if res.status_code != 200:
            logger.error(f"       [!] AI Error {res.status_code}: {res.text}")
            return []

        raw_ai_text = res.json()['candidates'][0]['content']['parts'][0]['text']
        # Extract JSON list from backticks if needed
        match = re.search(r'\[\s*{.*}\s*\]', raw_ai_text, re.DOTALL)
        json_str = match.group(0) if match else raw_ai_text
        stories = json.loads(json_str)
        
        with open(cache_path, 'w', encoding='utf-8') as f: json.dump(stories, f)
        return stories
    except Exception as e:
        logger.error(f"       [!] AI Logic failed: {e}")
        return []

def get_video_ids_stealth(channel_id):
    url = f"https://www.youtube.com/channel/{channel_id}/videos"
    try:
        r = requests.get(url, headers=HEADERS, timeout=8)
        if r.status_code == 200:
            return list(dict.fromkeys(re.findall(r'"videoId":"([^"]+)"', r.text)))[:5]
    except: pass
    return []

def main():
    parser = argparse.ArgumentParser(); parser.add_argument("--debug", action="store_true"); args = parser.parse_args()
    setup_logging(args.debug)
    
    config = load_and_validate_config()
    check_environment(config.get('output_dir'))
    
    conn = sqlite3.connect(DB_FILE); cursor = conn.cursor()
    conn.execute('CREATE TABLE IF NOT EXISTS videos (video_id TEXT PRIMARY KEY, added DATETIME)')
    
    sd_client = Supadata(api_key=config['supadata_api_key'])
    logger.info(f"TubeNews session started. Monitoring {len(config.get('feeds', []))} feeds.")

    for feed in config['feeds']:
        logger.info(f"[*] Feed: {feed['channel_name']}")
        v_ids = get_video_ids_stealth(str(feed['channel_id']).strip())
        
        if not v_ids:
            logger.warning(f"    Discovery blocked for this feed. Moving on...")
            continue

        for v_id in v_ids:
            cursor.execute("SELECT 1 FROM videos WHERE video_id=?", (v_id,))
            if cursor.fetchone(): continue
            
            logger.info(f" [+] Found: {v_id}")
            
            # Start actual work
            transcript = get_transcript(v_id, sd_client)
            if not transcript: continue

            stories = generate_news(transcript, feed['slant'], v_id, config['gemini_api_key'], config['ai_model'])
            
            # None means Quota stop, empty list means no stories
            if stories is None: break 
            
            if stories:
                # Update RSS Logic
                safe_name = re.sub(r'[^a-zA-Z0-9_\-]', '_', feed['channel_name'])
                f_path = os.path.join(config['output_dir'], f"{safe_name}.xml")
                fg = FeedGenerator()
                fg.id(v_id); fg.title(f"News: {feed['channel_name']}"); fg.link(href=f"https://youtube.com/watch?v={v_id}")
                fg.description(f"AI slant: {feed['slant']}")
                for s in stories:
                    fe = fg.add_entry()
                    link = f"https://youtu.be/{v_id}?t={s.get('start_time_seconds',0)}"
                    fe.title(s['title']); fe.link(href=link); fe.content(s['content'])
                fg.rss_file(f_path, pretty=True)
                logger.info(f"       [✓] Published {len(stories)} articles.")
            else:
                logger.info("       [-] No stories matched slant.")

            cursor.execute("INSERT INTO videos VALUES (?, ?)", (v_id, datetime.now()))
            conn.commit()

    conn.close()
    logger.info("Session complete.")

if __name__ == "__main__": main()
