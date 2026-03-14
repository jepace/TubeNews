import os, json, requests, re, time, argparse, logging, socket, sys, hashlib
from pathlib import Path
from supadata import Supadata
from feedgen.feed import FeedGenerator
from datetime import datetime, timedelta

# --- ENVIRONMENT & PATHS ---
BASE_DIR = Path(__file__).resolve().parent
CONFIG_FILE = BASE_DIR / "TubeNews.json"
STORAGE_ROOT = BASE_DIR / "archive"

os.environ['SSL_CERT_FILE'] = '/usr/local/share/certs/ca-root-nss.crt'
socket.setdefaulttimeout(15) 
HEADERS = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/122.0.0.0 Safari/537.36'}

logging.getLogger("urllib3").setLevel(logging.WARNING)
logging.getLogger("requests").setLevel(logging.WARNING)
logger = logging.getLogger("TubeNews")

AI_DISABLED = False

def setup_logging(debug_mode):
    level = logging.DEBUG if debug_mode else logging.INFO
    logging.basicConfig(
        level=level,
        format='%(asctime)s %(levelname)s: %(message)s',
        datefmt='%H:%M:%S'
    )

def slugify(text): return re.sub(r'[^a-zA-Z0-9]', '_', text).strip('_')

def get_transcript_and_meta(video_id, sd_client):
    url = f"https://www.youtube.com/watch?v={video_id}"
    logger.info("--> Step 2: Fetching transcript + metadata from Supadata...")
    logger.debug(f"Connecting to api.supadata.ai for ID: {video_id}...")
    try:
        t_obj = sd_client.transcript(url=url, text=False)
        title = getattr(t_obj, 'metadata', {}).get('title', video_id)
        raw_date = getattr(t_obj, 'metadata', {}).get('publishDate', '') or getattr(t_obj, 'metadata', {}).get('publishedAt', '')
        actual_date = raw_date.split('T')[0] if raw_date else datetime.now().strftime('%Y-%m-%d')
        
        if hasattr(t_obj, 'content') and t_obj.content:
            text = "\n".join([f"{int(getattr(s, 'offset', 0)/1000)}s --> {getattr(s, 'text', '')}" for s in t_obj.content])
            logger.debug(f"Supadata successfully returned {len(t_obj.content)} segments.")
            return text, title, actual_date
    except Exception as e:
        logger.error(f"[!] Supadata call failed: {e}")
    return None, None, None

def generate_news(transcript, focus, video_title, v_date, meeting_dir, gemini_key, model_name):
    global AI_DISABLED
    if AI_DISABLED: return None
    
    if list(meeting_dir.glob("[0-9]*.md")):
        logger.debug(f"Analysis already exists. Skipping AI call.")
        return []

    logger.info(f"--> Step 3: AI Analysis for: {video_title[:40]}...")
    logger.debug(f"Connecting to Google Gemini v1 REST API (timeout 120s)...")
    
    url = f"https://generativelanguage.googleapis.com/v1/models/{model_name}:generateContent?key={gemini_key}"
    prompt = (f"Analyze transcript of '{video_title}' on {v_date}. Focus: {focus}. "
              "Return result ONLY as raw JSON list of objects: [{'title': '...', 'dateline': '...', 'content': '...', 'start_time_seconds': 123}]")
    payload = {"contents": [{"parts": [{"text": f"{prompt}\n\nTRANSCRIPT:\n{transcript[:100000]}"}]}]}
    
    try:
        res = requests.post(url, json=payload, timeout=150)
        if res.status_code == 200:
            raw = res.json()['candidates'][0]['content']['parts'][0]['text']
            match = re.search(r'\[\s*{.*}\s*\]', raw, re.DOTALL)
            stories = json.loads(match.group(0) if match else raw)
            
            for i, s in enumerate(stories, 1):
                safe_title = slugify(s['title'])[:40]
                with open(meeting_dir / f"{i:02d}_{safe_title}.md", 'w', encoding='utf-8') as f:
                    f.write(f"# {s['title']}\n*{s.get('dateline', 'California')}*\n\n{s['content']}")
            return stories
        elif res.status_code == 429:
            logger.warning("[!] Gemini Rate Limit reached. AI disabled for session.")
            AI_DISABLED = True
    except: pass
    return None

def discover_video_ids(channel_id):
    all_ids = []
    for tab in ["videos", "streams"]:
        url = f"https://www.youtube.com/channel/{channel_id}/{tab}"
        logger.debug(f"Connecting to YouTube tab: {tab}")
        try:
            r = requests.get(url, headers=HEADERS, timeout=10)
            if r.status_code == 200:
                found = re.findall(r'"videoId":"([^"]{11})"', r.text)
                all_ids.extend(found)
        except Exception as e:
            logger.debug(f"Tab '{tab}' failed: {e}")
    return list(dict.fromkeys(all_ids))

def rebuild_feed(feed_dir, feed_cfg):
    safe_name = slugify(feed_cfg['channel_name'])
    logger.info(f"--> Step 4: Finalizing RSS for {safe_name}")
    fg = FeedGenerator()
    fg.id(f"TubeNews_{safe_name}"); fg.title(f"TubeNews: {feed_cfg['channel_name']}")
    fg.description(f"Focus: {feed_cfg['focus']}")
    fg.link(href=f"https://www.youtube.com/channel/{feed_cfg['channel_id']}", rel='alternate')

    m_dirs = sorted([d for d in feed_dir.iterdir() if d.is_dir()], reverse=True)
    count = 0
    for m_dir in m_dirs:
        meta_p = m_dir / "metadata.json"
        if not meta_p.exists(): continue
        meta = json.loads(meta_p.read_text())
        if meta.get('status') == 'ignored_too_old': continue
        
        for s_file in sorted(list(m_dir.glob("[0-9]*.md"))):
            raw = s_file.read_text(encoding='utf-8').splitlines()
            title, date = raw[0].replace('# ', ''), raw[1].replace('*', '')
            content_body = "\n".join(raw[2:]).replace('\n', '<br>')
            
            fe = fg.add_entry()
            fe.id(hashlib.md5(s_file.read_text(encoding='utf-8').encode()).hexdigest())
            fe.title(f"{title} | {meta.get('video_title', 'Meeting')}")
            fe.link(href=f"https://youtu.be/{meta['video_id']}")
            fe.content(f"<strong>{date}</strong><br><br>{content_body}", type='html')
            fe.published(datetime.fromtimestamp(meta['processed_at']).astimezone())
            count += 1
            if count >= 50: break
        if count >= 50: break
    fg.rss_file(feed_dir / "feed.xml", pretty=True)

def main():
    parser = argparse.ArgumentParser(); parser.add_argument("--debug", action="store_true"); args = parser.parse_args()
    setup_logging(args.debug)
    with open(CONFIG_FILE, 'r') as f: config = json.load(f)
    sd_client = Supadata(api_key=config['supadata_api_key'])
    logger.info(f"Session Start | AI Model: {config.get('ai_model')}")

    for feed in config['feeds']:
        chan_slug = slugify(feed['channel_name'])
        logger.info(f"[*] Feed: {feed['channel_name']}")
        feed_dir = STORAGE_ROOT / chan_slug
        
        content_changed = not (feed_dir / "feed.xml").exists()
        is_new_feed = not feed_dir.exists()
        feed_dir.mkdir(parents=True, exist_ok=True)

        all_ids = discover_video_ids(feed['channel_id'])
        if not all_ids: continue
        
        new_ids = [v for v in all_ids if not any(d.name.endswith(v) for d in feed_dir.iterdir() if d.is_dir())]

        if not new_ids:
            logger.info("--> Step 1: No new videos discovered.")
        else:
            status_msg = f"--> Step 1: Found {len(new_ids)} new videos."
            if is_new_feed: status_msg += " New feed: processing 1, archiving rest as legacy."
            logger.info(status_msg)

        for idx, v_id in enumerate(new_ids):
            if is_new_feed and idx > 0:
                # FIXED: Consistent with catchup.py (1900-01-01)
                m_dir = feed_dir / f"1900-01-01_{v_id}"
                m_dir.mkdir(exist_ok=True)
                (m_dir / "metadata.json").write_text(json.dumps({"video_id": v_id, "status": "ignored_too_old", "processed_at": time.time()}))
                content_changed = True
                continue

            logger.info(f"[+] Processing: {v_id}")
            transcript, v_title, v_date_str = get_transcript_and_meta(v_id, sd_client)
            if not transcript: continue

            m_dir = feed_dir / f"{v_date_str}_{v_id}"
            m_dir.mkdir(exist_ok=True)
            (m_dir / "transcript.txt").write_text(transcript, encoding='utf-8')

            stories = generate_news(transcript, feed['focus'], v_title, v_date_str, m_dir, config['gemini_api_key'], config['ai_model'])
            
            if stories is not None:
                if not stories: logger.info("    [-] No focus match found.")
                else: logger.info(f"    [✓] Generated {len(stories)} stories.")
                
                meta = {"video_id": v_id, "video_title": v_title, "status": "processed", "processed_at": time.time()}
                (m_dir / "metadata.json").write_text(json.dumps(meta))
                content_changed = True

        if content_changed:
            rebuild_feed(feed_dir, feed)
        else:
            logger.info("--> Step 4: Skipping RSS rebuild (No changes).")

    logger.info("Session End.")

if __name__ == "__main__": main()
