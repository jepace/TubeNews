import os, json, requests, re, time, argparse, logging, socket, sys, hashlib, shutil
from pathlib import Path
from supadata import Supadata
from feedgen.feed import FeedGenerator
from datetime import datetime, timedelta

# --- ENVIRONMENT & PATHS ---
BASE_DIR = Path(__file__).resolve().parent
CONFIG_FILE = BASE_DIR / "TubeNews.json"
STORAGE_ROOT = BASE_DIR / "archive"

# FreeBSD specific SSL pathing (no-op on Linux/macOS where this path doesn't exist)
_FREEBSD_CERT = '/usr/local/share/certs/ca-root-nss.crt'
if os.path.exists(_FREEBSD_CERT):
    os.environ['SSL_CERT_FILE'] = _FREEBSD_CERT
socket.setdefaulttimeout(15) 
HEADERS = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/122.0.0.0 Safari/537.36'}

# Silence low-level library noise
logging.getLogger("urllib3").setLevel(logging.WARNING)
logging.getLogger("requests").setLevel(logging.WARNING)
logger = logging.getLogger("TubeNews")

def setup_logging(debug_mode):
    level = logging.DEBUG if debug_mode else logging.INFO
    logging.basicConfig(
        level=level,
        format='%(asctime)s %(levelname)s: %(message)s',
        datefmt='%H:%M:%S'
    )

def slugify(text):
    """Convert text to a filesystem-safe slug (non-alphanumeric chars → underscores)."""
    return re.sub(r'[^a-zA-Z0-9]', '_', text).strip('_')

def get_transcript_and_meta(video_id, sd_client):
    """Scrapes YouTube page for title and date; fetches transcript from Supadata."""
    url = f"https://www.youtube.com/watch?v={video_id}"
    logger.info("--> Step 2: Requesting transcript + metadata from Supadata...")
    logger.debug(f"Connecting to api.supadata.ai for ID: {video_id} (Token required)")

    # Scrape title and upload date from YouTube page (free, no API token)
    title = video_id
    actual_date = datetime.now().strftime('%Y-%m-%d')
    try:
        r = requests.get(url, headers=HEADERS, timeout=10)
        if r.status_code == 200:
            date_match = re.search(r'"uploadDate":"(\d{4}-\d{2}-\d{2})"', r.text)
            if date_match:
                actual_date = date_match.group(1)
            title_match = re.search(r'<title>(.+?) - YouTube</title>', r.text)
            if title_match:
                title = title_match.group(1)
    except Exception as e:
        logger.debug(f"YouTube page scrape failed for {video_id}: {e}")

    # Fetch transcript segments from Supadata
    try:
        t_obj = sd_client.transcript(url=url, text=False)
        if hasattr(t_obj, 'content') and t_obj.content:
            text = "\n".join([f"{int(getattr(s, 'offset', 0)/1000)}s --> {getattr(s, 'text', '')}" for s in t_obj.content])
            logger.debug(f"Supadata successfully returned {len(t_obj.content)} segments.")
            return text, title, actual_date
    except Exception as e:
        logger.error(f"[!] Supadata call failed: {e}")
    return None, None, None

def generate_news(transcript, focus, video_title, v_date, meeting_dir, gemini_key, model_name):
    """Reporter logic using the high-quality editorial directive.

    Returns a list of story dicts on success, False if rate-limited (caller
    should disable AI for the session), or None on any other failure.
    """
    # Clean up any existing stories if we are rerunning
    for old_story in meeting_dir.glob("[0-9]*.md"):
        old_story.unlink()

    logger.info(f"--> Step 3: AI Analysis via {model_name}...")
    logger.debug(f"Connecting to Google Gemini v1 REST API for: {video_title[:30]}...")
    
    url = f"https://generativelanguage.googleapis.com/v1/models/{model_name}:generateContent?key={gemini_key}"
    
    # Restored the full journalistic prompt
    directive = (
        f"You are a highly experienced investigative reporter specializing in local government. "
        f"Analyze this transcript of '{video_title}' held on {v_date}.\n\n"
        f"OBJECTIVE: Identify and extract distinct news stories strictly relevant to this FOCUS: '{focus}'.\n\n"
        "WRITING GUIDELINES:\n"
        "1. TONE: Professional, objective, and authoritative. Use the Inverted Pyramid style.\n"
        "2. CONTENT: Focus on the 'Why it Matters'. Skip ceremonial talk. "
        "Highlight specific project addresses, vote counts, and fiscal impacts.\n"
        "3. DATELINE: Construct a formal AP-style dateline (e.g., 'GONZALES, Calif. — March 14, 2026').\n\n"
        "Return result ONLY as raw JSON list of objects with keys: 'title', 'dateline', 'content', 'start_time_seconds'."
    )
    
    if len(transcript) > 100000:
        logger.warning(f"Transcript for '{video_title[:30]}' is {len(transcript):,} chars; truncating to 100,000.")
    payload = {"contents": [{"parts": [{"text": f"{directive}\n\nTRANSCRIPT:\n{transcript[:100000]}"}]}]}
    
    try:
        res = requests.post(url, json=payload, timeout=150)
        if res.status_code == 200:
            raw = res.json()['candidates'][0]['content']['parts'][0]['text']
            match = re.search(r'\[\s*{.*}\s*\]', raw, re.DOTALL)
            stories = json.loads(match.group(0) if match else raw)
            
            for i, s in enumerate(stories, 1):
                safe_title = slugify(s['title'])[:40]
                with open(meeting_dir / f"{i:02d}_{safe_title}.md", 'w', encoding='utf-8') as f:
                    f.write(f"# {s['title']}\n")
                    f.write(f"*{s.get('dateline', 'California')}*\n\n")
                    f.write(f"{s['content']}\n\n")
                    f.write(f"---\n")
                    f.write(f"**Segment Start:** {s.get('start_time_seconds', 0)}s\n")
            logger.debug(f"AI successfully generated {len(stories)} stories.")
            return stories
        elif res.status_code == 429:
            logger.warning("[!] Gemini Rate Limit. AI disabled for this run.")
            return False
    except Exception as e:
        logger.error(f"[!] AI logic failure: {e}")
    return None

def discover_video_ids(channel_id):
    """Scrape the channel's videos and streams tabs; return deduplicated list of video IDs."""
    all_ids = []
    for tab in ["videos", "streams"]:
        url = f"https://www.youtube.com/channel/{channel_id}/{tab}"
        logger.debug(f"Discovery: Fetching YouTube tab: {tab}...")
        try:
            r = requests.get(url, headers=HEADERS, timeout=10)
            if r.status_code == 200:
                found = re.findall(r'"videoId":"([^"]{11})"', r.text)
                if not found:
                    logger.warning(f"Discovery: Got 200 from '{tab}' tab but found 0 video IDs. YouTube HTML structure may have changed.")
                all_ids.extend(found)
        except Exception as e:
            logger.debug(f"Discovery: Tab {tab} failed: {e}")
    return list(dict.fromkeys(all_ids))

def rebuild_feed(feed_dir, feed_cfg):
    """Generate archive/<council>/rss.xml with up to 50 most recent stories."""
    safe_name = slugify(feed_cfg['channel_name'])
    logger.info(f"--> Step 4: Rebuilding RSS for {safe_name}")
    fg = FeedGenerator()
    fg.id(f"tubenews_{safe_name}")
    fg.title(f"TubeNews: {feed_cfg['channel_name']}")
    fg.description(f"Expert focus: {feed_cfg['focus']}")
    fg.link(href=f"https://www.youtube.com/channel/{feed_cfg['channel_id']}", rel='alternate')

    m_dirs = sorted([d for d in feed_dir.iterdir() if d.is_dir()], reverse=True)
    count = 0
    for m_dir in m_dirs:
        if count >= 50: break
        meta_p = m_dir / "metadata.json"
        if not meta_p.exists(): continue
        meta = json.loads(meta_p.read_text())
        if meta.get('status') == 'ignored_too_old': continue

        for s_file in sorted(list(m_dir.glob("[0-9]*.md"))):
            if count >= 50: break
            text = s_file.read_text(encoding='utf-8')
            lines = text.splitlines()
            title, dateline = lines[0].replace('# ', ''), lines[1].replace('*', '')
            body_text = "<br>".join(lines[2:]).replace('\n', '<br>')
            ts_match = re.search(r'\*\*Segment Start:\*\* (\d+)s', text)
            ts = ts_match.group(1) if ts_match else "0"
            fe = fg.add_entry()
            fe.id(hashlib.md5(text.encode()).hexdigest())
            fe.title(f"{title} | {meta.get('video_title', 'Meeting')}")
            fe.link(href=f"https://youtu.be/{meta['video_id']}?t={ts}")
            fe.content(f"<strong>{dateline}</strong><br><br>{body_text}", type='html')
            fe.published(datetime.fromtimestamp(meta['processed_at']).astimezone())
            count += 1
    fg.rss_file(feed_dir / "rss.xml", pretty=True)

def rebuild_meta_feed(base_url=""):
    """Aggregate stories from all council folders into archive/rss.xml.

    Args:
        base_url: Public URL of the meta-feed (used as the RSS self-link).
                  If empty, the self-link is omitted.
    """
    logger.info("--> Step 5: Rebuilding Regional Meta-Feed (archive/rss.xml)...")
    fg = FeedGenerator()
    fg.id("tubenews_meta_rss")
    fg.title("TubeNews: Regional Real Estate & Development")
    fg.description("Aggregated regional reporting.")
    fg.link(href=base_url if base_url else "https://www.youtube.com", rel='alternate')
    if base_url:
        fg.link(href=base_url, rel='self')

    all_stories = []
    for council_dir in [d for d in STORAGE_ROOT.iterdir() if d.is_dir()]:
        for m_dir in [d for d in council_dir.iterdir() if d.is_dir()]:
            meta_p = m_dir / "metadata.json"
            if not meta_p.exists(): continue
            try:
                meta = json.loads(meta_p.read_text())
                if meta.get('status') == 'ignored_too_old': continue
                for s_file in m_dir.glob("[0-9]*.md"):
                    all_stories.append({'file': s_file, 'meta': meta, 'council': council_dir.name.replace('_', ' ')})
            except Exception:
                continue

    all_stories.sort(key=lambda x: x['meta'].get('processed_at', 0), reverse=True)

    for entry in all_stories[:100]:
        try:
            raw_text = entry['file'].read_text(encoding='utf-8')
            raw = raw_text.splitlines()
            title, date = raw[0].replace('# ', ''), raw[1].replace('*', '')
            body_text = "<br>".join(raw[2:]).replace('\n', '<br>')
            ts_match = re.search(r'\*\*Segment Start:\*\* (\d+)s', raw_text)
            ts = ts_match.group(1) if ts_match else "0"
            fe = fg.add_entry()
            fe.id(hashlib.md5(raw_text.encode()).hexdigest())
            fe.title(f"[{entry['council']}] {title}")
            fe.link(href=f"https://youtu.be/{entry['meta']['video_id']}?t={ts}")
            fe.content(f"<strong>{date}</strong><br><em>Source: {entry['council']}</em><br><br>{body_text}", type='html')
            fe.published(datetime.fromtimestamp(entry['meta'].get('processed_at', time.time())).astimezone())
        except Exception:
            continue

    fg.rss_file(STORAGE_ROOT / "rss.xml", pretty=True)

def main():
    """Entry point: load config, process each feed, and rebuild RSS outputs."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args()
    setup_logging(args.debug)
    with open(CONFIG_FILE, 'r') as f:
        config = json.load(f)
    sd_client = Supadata(api_key=config['supadata_api_key'])
    logger.info(f"Session Start | AI Model: {config.get('ai_model')}")
    
    any_content_changed = False
    ai_disabled = False

    for feed in config['feeds']:
        chan_slug = slugify(feed['channel_name'])
        logger.info(f"[*] Feed: {feed['channel_name']}")
        feed_dir = STORAGE_ROOT / chan_slug
        
        feed_content_changed = not (feed_dir / "rss.xml").exists()
        is_new_feed = not feed_dir.exists()
        feed_dir.mkdir(parents=True, exist_ok=True)

        all_ids = discover_video_ids(feed['channel_id'])
        if not all_ids: continue
        
        # Determine which videos are truly new to the filesystem
        new_ids = [v for v in all_ids if not any(d.name.endswith(v) for d in feed_dir.iterdir() if d.is_dir() and (d / "metadata.json").exists())]

        if not new_ids:
            logger.info("--> Step 1: No new videos discovered.")
        else:
            logger.info(f"--> Step 1: Found {len(new_ids)} videos to check.")

        for idx, v_id in enumerate(all_ids):
            # Find existing directory for this ID if it exists
            existing_dir = next((d for d in feed_dir.iterdir() if d.is_dir() and d.name.endswith(v_id)), None)
            
            # IF FULLY DONE (metadata exists), SKIP
            if existing_dir and (existing_dir / "metadata.json").exists():
                continue

            # AUTO-CATCHUP FOR NEW FEEDS
            if is_new_feed and idx > 0:
                m_dir = feed_dir / f"1900-01-01_{v_id}"
                m_dir.mkdir(exist_ok=True)
                (m_dir / "metadata.json").write_text(json.dumps({"video_id": v_id, "status": "ignored_too_old", "processed_at": time.time()}))
                feed_content_changed = True
                continue

            # LOAD TRANSCRIPT (LOCAL FIRST)
            transcript = None
            v_title = v_id
            v_date_str = "1900-01-01"

            if existing_dir and (existing_dir / "transcript.txt").exists():
                logger.info(f"[✓] Found local transcript for {v_id}. Re-running AI only.")
                transcript = (existing_dir / "transcript.txt").read_text(encoding='utf-8')
                v_date_str = existing_dir.name.split('_')[0]
                m_dir = existing_dir
            else:
                # TRULY NEW or Folder missing
                logger.info(f"[+] Processing: {v_id}")
                transcript, v_title, v_date_str = get_transcript_and_meta(v_id, sd_client)
                if not transcript: continue
                m_dir = feed_dir / f"{v_date_str}_{v_id}"
                m_dir.mkdir(exist_ok=True)
                (m_dir / "transcript.txt").write_text(transcript, encoding='utf-8')

            # RUN AI
            if not ai_disabled:
                stories = generate_news(transcript, feed['focus'], v_title, v_date_str, m_dir, config['gemini_api_key'], config['ai_model'])
                if stories is False:
                    ai_disabled = True
                elif stories is not None:
                    meta = {"video_id": v_id, "video_title": v_title, "status": "processed", "processed_at": time.time()}
                    (m_dir / "metadata.json").write_text(json.dumps(meta))
                    feed_content_changed = True

        if feed_content_changed:
            rebuild_feed(feed_dir, feed)
            any_content_changed = True

    if any_content_changed or not (STORAGE_ROOT / "rss.xml").exists():
        rebuild_meta_feed(base_url=config.get('base_url', ''))

    logger.info("Session End.")

if __name__ == "__main__": main()
