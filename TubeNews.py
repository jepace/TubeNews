#!/usr/bin/env python3
"""TubeNews — monitor YouTube channels for new videos and generate news feeds.

Workflow for each configured channel (feed):
  1. Discover recent video IDs by scraping YouTube channel pages.
  2. Skip videos already in the local archive.
  3. For genuinely new videos: fetch a transcript via the Supadata API and
     scrape basic metadata (title, upload date) from the YouTube watch page.
  4. Send the transcript to Google Gemini so it can extract focused news
     stories and write each story as a Markdown file.
  5. Rebuild the per-channel RSS feed and the site-wide meta-feed.

Run:
    python TubeNews.py [--debug]

Configuration lives in TubeNews.json (see TubeNews.json.sample).
"""

# ---------------------------------------------------------------------------
# Standard-library imports
# ---------------------------------------------------------------------------
import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import logging
import os
import re
import socket
import threading
import time
from datetime import date, datetime, timedelta
from pathlib import Path

# ---------------------------------------------------------------------------
# Third-party imports
# ---------------------------------------------------------------------------
import requests
from feedgen.feed import FeedGenerator
from supadata import Supadata

# ---------------------------------------------------------------------------
# Environment & paths
# ---------------------------------------------------------------------------

BASE_DIR = Path(__file__).resolve().parent
CONFIG_FILE = BASE_DIR / "TubeNews.json"
STORAGE_ROOT = BASE_DIR / "archive"

# FreeBSD ships its CA bundle in a non-standard location; tell Python where
# to find it so HTTPS requests succeed. On Linux/macOS this path won't exist
# and the assignment is skipped.
_FREEBSD_CERT = "/usr/local/share/certs/ca-root-nss.crt"
if os.path.exists(_FREEBSD_CERT):
    os.environ["SSL_CERT_FILE"] = _FREEBSD_CERT

# A short default timeout prevents the script hanging indefinitely when
# YouTube or an API endpoint is slow to respond.
socket.setdefaulttimeout(15)

# Mimic a real browser so YouTube doesn't serve a bot-detection page instead
# of the normal HTML (which contains the metadata we need to scrape).
YOUTUBE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "Chrome/122.0.0.0 Safari/537.36"
    )
}

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

# Keep third-party chatter out of the output; only TubeNews messages matter.
logging.getLogger("urllib3").setLevel(logging.WARNING)
logging.getLogger("requests").setLevel(logging.WARNING)
logger = logging.getLogger("TubeNews")


def setup_logging(debug_mode: bool) -> None:
    """Configure root logging level and format.

    Args:
        debug_mode: When True, emit DEBUG messages (API calls, raw responses).
                    When False, emit INFO and above (normal operational output).
    """
    level = logging.DEBUG if debug_mode else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s: %(message)s",
        datefmt="%H:%M:%S",
    )


# ---------------------------------------------------------------------------
# Utility helpers
# ---------------------------------------------------------------------------


def slugify(text: str) -> str:
    """Convert *text* to a filesystem-safe slug.

    Every character that isn't a letter or digit is replaced with an
    underscore, then leading/trailing underscores are stripped.

    Examples:
        >>> slugify("City Council")
        'City_Council'
        >>> slugify("---test---")
        'test'
    """
    return re.sub(r"[^a-zA-Z0-9]", "_", text).strip("_")


def parse_story_file(story_path: Path) -> dict:
    """Read a story Markdown file and return its structured fields.

    Story files are written by :func:`write_story_files` in a fixed format::

        # Story Title
        *AP-style dateline*
        **Source:** https://youtu.be/video_id?t=120

        Body paragraphs …

        ---
        **Segment Start:** 120s

    Returns a dict with keys:
        title        – headline text (string)
        dateline     – AP dateline (string)
        body_html    – body lines joined with ``<br>`` tags (string)
        start_seconds – integer timestamp into the source video
    """
    text = story_path.read_text(encoding="utf-8")
    lines = text.splitlines()

    title = lines[0].replace("# ", "")
    dateline = lines[1].replace("*", "")
    body_lines = [
        l for l in lines[2:]
        if l.strip() != "---" and not l.startswith("**Segment Start:**") and not l.startswith("**Source:**")
    ]
    body_html = "<br>".join(body_lines).replace("\n", "<br>")

    timestamp_match = re.search(r"\*\*Segment Start:\*\* (\d+)s", text)
    start_seconds = int(timestamp_match.group(1)) if timestamp_match else 0

    return {
        "title": title,
        "dateline": dateline,
        "body_html": body_html,
        "start_seconds": start_seconds,
        # Keep a hash of the raw text so feed entry IDs are stable across runs.
        "content_hash": hashlib.md5(text.encode()).hexdigest(),
    }


# ---------------------------------------------------------------------------
# YouTube data-gathering
# ---------------------------------------------------------------------------


def _relative_date_to_iso(text: str) -> str:
    """Convert a YouTube relative-date string to ``YYYY-MM-DD``.

    YouTube's channel listing page provides publication dates as relative
    strings (e.g. ``"11 days ago"``, ``"2 weeks ago"``).  We subtract the
    implied offset from today to get an approximate calendar date.

    For completed live-streams YouTube sometimes provides an exact date
    (e.g. ``"Streamed live on Feb 24, 2026"``); we parse that precisely.

    Falls back to today's date for any unrecognised format.
    """
    today = datetime.now()
    lower = text.lower().strip()

    # "Streamed live on Month DD, YYYY" or just "Month DD, YYYY"
    exact = re.search(r"([a-z]+ \d{1,2},\s*\d{4})", lower)
    if exact:
        try:
            return datetime.strptime(exact.group(1), "%b %d, %Y").strftime("%Y-%m-%d")
        except ValueError:
            pass

    # "N seconds/minutes/hours/days/weeks/months/years ago"
    m = re.match(r"(\d+)\s+(second|minute|hour|day|week|month|year)s?\s+ago", lower)
    if m:
        n, unit = int(m.group(1)), m.group(2)
        days = {"second": 0, "minute": 0, "hour": 0, "day": n,
                "week": n * 7, "month": n * 30, "year": n * 365}[unit]
        return (today - timedelta(days=days)).strftime("%Y-%m-%d")

    return today.strftime("%Y-%m-%d")


def _parse_channel_page_metadata(html: str) -> dict[str, dict]:
    """Extract per-video metadata from the ``ytInitialData`` JSON in a channel page.

    YouTube embeds a large JSON blob (``var ytInitialData = {...};``) that
    powers the video-card grid.  Each card's ``videoRenderer`` object contains
    the video ID, title, relative publish date, and live-stream status.

    Returns a ``{videoId: {title, date, is_live}}`` mapping.
    Falls back to an empty dict if the JSON blob is absent or unparseable.
    """
    result: dict[str, dict] = {}

    m = re.search(
        r"var ytInitialData\s*=\s*(\{.*?\});\s*(?:var |</script>)",
        html,
        re.DOTALL,
    )
    if not m:
        return result

    try:
        data = json.loads(m.group(1))
    except json.JSONDecodeError:
        return result

    def walk(obj: object) -> None:
        if isinstance(obj, dict):
            if "videoId" in obj and "title" in obj:
                vid = obj["videoId"]
                runs = obj["title"].get("runs", [])
                title = runs[0].get("text", "") if runs else ""

                relative = obj.get("publishedTimeText", {}).get("simpleText", "")
                date = _relative_date_to_iso(relative) if relative else datetime.now().strftime("%Y-%m-%d")

                is_live = any(
                    ov.get("thumbnailOverlayTimeStatusRenderer", {}).get("style")
                    in ("LIVE", "UPCOMING")
                    for ov in obj.get("thumbnailOverlays", [])
                )

                if vid not in result:
                    result[vid] = {"title": title, "date": date, "is_live": is_live}

            for v in obj.values():
                walk(v)
        elif isinstance(obj, list):
            for item in obj:
                walk(item)

    walk(data)
    return result


def discover_videos(channel_id: str, feed_name: str = "") -> list[dict]:
    """Scrape a channel's *videos* and *streams* tabs; return video metadata.

    Both tabs are fetched concurrently.  Results are merged in a fixed order
    (videos tab first, then streams) so the returned list is stable.

    Parses the ``ytInitialData`` JSON blob embedded in each tab's HTML to
    extract the video ID, title, approximate upload date, and live status for
    every visible video.  The simple ``videoId`` regex is also run as a
    fallback to ensure no IDs are missed if the JSON parse fails.

    Returns an ordered list of dicts (most-recent first, duplicates removed)::

        {"id": str, "title": str, "date": "YYYY-MM-DD", "is_live": bool}
    """
    tabs = ["videos", "streams"]
    prefix = f"{feed_name}: " if feed_name else ""

    def _fetch_tab(tab: str) -> tuple[str, str | None]:
        url = f"https://www.youtube.com/channel/{channel_id}/{tab}"
        for attempt in range(3):
            logger.debug(f"{prefix}YouTube: Fetching {tab} tab" + (f" (retry {attempt})" if attempt else ""))
            try:
                response = requests.get(url, headers=YOUTUBE_HEADERS, timeout=15)
                if response.status_code == 200:
                    return tab, response.text
            except Exception as exc:
                if attempt < 2:
                    time.sleep(2 ** attempt)
                else:
                    logger.warning(f"{prefix}YouTube: {tab} tab failed after 3 attempts: {exc}")
        return tab, None

    with ThreadPoolExecutor(max_workers=2) as executor:
        tab_results = dict(executor.map(lambda t: _fetch_tab(t), tabs))

    all_ids: list[str] = []
    meta_lookup: dict[str, dict] = {}

    for tab in tabs:
        html = tab_results.get(tab)
        if html is None:
            continue
        meta_lookup.update(_parse_channel_page_metadata(html))
        found = re.findall(r'"videoId":"([^"]{11})"', html)
        if not found:
            logger.warning(
                f"{prefix}YouTube: Got 200 from {tab} tab but found 0 "
                "video IDs — YouTube HTML structure may have changed."
            )
        all_ids.extend(found)

    today = datetime.now().strftime("%Y-%m-%d")
    seen: dict[str, dict] = {}
    for vid in all_ids:
        if vid not in seen:
            m = meta_lookup.get(vid, {})
            seen[vid] = {
                "id": vid,
                "title": m.get("title") or vid,
                "date": m.get("date") or today,
                "is_live": m.get("is_live", False),
            }
    return list(seen.values())



def fetch_transcript(
    video_id: str,
    supadata_client: Supadata,
    feed_name: str = "",
    video_title: str = "",
) -> str | None:
    """Fetch timed transcript segments from the Supadata API.

    Each segment is formatted as ``"<offset_seconds>s --> <text>"`` so Gemini
    knows where each sentence occurs in the video timeline.

    Returns the formatted transcript string, or None if the API call fails.
    """
    prefix = ": ".join(p for p in [feed_name, video_title] if p)
    prefix = f"{prefix}: " if prefix else ""

    url = f"https://www.youtube.com/watch?v={video_id}"
    try:
        transcript_response = supadata_client.transcript(url=url, text=False)
        if hasattr(transcript_response, "content") and transcript_response.content:
            segments = transcript_response.content
            lines = [
                f"{int(getattr(seg, 'offset', 0) / 1000)}s --> {getattr(seg, 'text', '')}"
                for seg in segments
            ]
            logger.debug(f"{prefix}Supadata: Received {len(segments)} segments")
            return "\n".join(lines)
    except Exception as exc:
        if "live streaming" in str(exc).lower():
            logger.warning(f"{prefix}Supadata: Live stream — transcript unavailable, will retry next run")
        else:
            logger.error(f"{prefix}Supadata: Call failed: {exc}")

    return None


# ---------------------------------------------------------------------------
# AI story generation
# ---------------------------------------------------------------------------


def call_gemini_api(
    transcript_text: str,
    focus: str,
    video_title: str,
    video_date: str,
    gemini_api_key: str,
    model_name: str,
    feed_name: str = "",
) -> list | bool | None:
    """Send a transcript to Google Gemini and parse the returned news stories.

    The prompt instructs Gemini to act as an investigative reporter and return
    a JSON list of story objects.  We ask for raw JSON (no markdown fences) so
    it can be parsed directly.

    Returns:
        list  – one dict per story on success.
        False – caller should disable AI for the remainder of this run because
                the API returned HTTP 429 (rate-limited / quota exhausted).
        None  – any other failure; the caller should skip this video.
    """
    api_url = (
        f"https://generativelanguage.googleapis.com/v1/models/"
        f"{model_name}:generateContent?key={gemini_api_key}"
    )

    directive = (
        f"You are a highly experienced investigative reporter. "
        f"Analyze this transcript of '{video_title}' recorded on {video_date}.\n\n"
        f"OBJECTIVE: Identify and extract distinct news stories strictly "
        f"relevant to this FOCUS: '{focus}'.\n\n"
        "WRITING GUIDELINES:\n"
        "1. TONE: Professional, objective, and authoritative. Use the Inverted "
        "Pyramid style.\n"
        "2. CONTENT: Focus on the 'Why it Matters'. Skip ceremonial talk.\n"
        "3. DATELINE: Construct a formal AP-style dateline "
        "(e.g., 'SPRINGFIELD, Mo. — March 14, 2026').\n\n"
        "Return result ONLY as raw JSON list of objects with keys: "
        "'title', 'dateline', 'content', 'start_time_seconds'."
    )

    payload = {
        "contents": [
            {"parts": [{"text": f"{directive}\n\nTRANSCRIPT:\n{transcript_text}"}]}
        ]
    }

    prefix = ": ".join(p for p in [feed_name, video_title] if p)
    prefix = f"{prefix}: " if prefix else ""

    try:
        response = requests.post(api_url, json=payload, timeout=150)
        if response.status_code == 200:
            raw_text = response.json()["candidates"][0]["content"]["parts"][0]["text"]
            json_match = re.search(r"\[\s*{.*}\s*\]", raw_text, re.DOTALL)
            if not json_match:
                logger.debug(f"{prefix}Gemini: No JSON in response — 0 stories")
                return []
            stories = json.loads(json_match.group(0))
            logger.debug(f"{prefix}Gemini: Generated {len(stories)} stories")
            return stories
        elif response.status_code == 429:
            logger.warning(f"{prefix}Gemini: Rate limit hit — AI disabled for this run")
            return False
    except Exception as exc:
        logger.error(f"{prefix}Gemini: API call failed: {exc}")

    return None


def write_story_files(stories: list, meeting_dir: Path, video_id: str = "") -> None:
    """Write each story dict as a numbered Markdown file inside *meeting_dir*.

    File names are ``01_<slug>.md``, ``02_<slug>.md``, …  Any stale story
    files from a previous (failed) run are deleted first to avoid mixing
    results from different Gemini calls.

    File format::

        # Story Title
        *AP-style dateline*
        **Source:** https://youtu.be/<video_id>?t=120

        Body text …

        ---
        **Segment Start:** 120s
    """
    # Remove leftovers from any prior run so we start clean.
    for old_file in meeting_dir.glob("[0-9]*.md"):
        old_file.unlink()

    for index, story in enumerate(stories, start=1):
        safe_title = slugify(story["title"])[:40]
        file_path = meeting_dir / f"{index:02d}_{safe_title}.md"
        with open(file_path, "w", encoding="utf-8") as fh:
            fh.write(f"# {story['title']}\n")
            fh.write(f"*{story.get('dateline', 'Local News')}*\n")
            if video_id:
                start_seconds = story.get('start_time_seconds', 0)
                fh.write(f"**Source:** https://youtu.be/{video_id}?t={start_seconds}\n")
            fh.write(f"\n{story['content']}\n\n")
            fh.write("---\n")
            fh.write(f"**Segment Start:** {story.get('start_time_seconds', 0)}s\n")


# ---------------------------------------------------------------------------
# RSS feed builders
# ---------------------------------------------------------------------------


def rebuild_feed(feed_dir: Path, feed_cfg: dict) -> None:
    """Regenerate ``<feed_dir>/rss.xml`` from all processed meetings.

    Includes all stories, sorted newest-meeting first.
    Meetings with ``status == "ignored_too_old"`` are skipped entirely so
    back-catalogue catch-up stubs don't pollute the feed.

    Args:
        feed_dir:  Root directory for this channel (e.g., ``archive/my_channel``).
        feed_cfg:  The feed's config dict from ``TubeNews.json`` (needs
                   ``channel_name``, ``channel_id``, and ``focus``).
    """
    safe_name = slugify(feed_cfg["channel_name"])
    logger.info(f"{feed_cfg['channel_name']}: TubeNews: Rebuilding RSS feed")

    feed = FeedGenerator()
    feed.id(f"tubenews_{safe_name}")
    feed.title(f"TubeNews: {feed_cfg['channel_name']}")
    feed.description(f"Expert focus: {feed_cfg['focus']}")
    feed.link(
        href=f"https://www.youtube.com/channel/{feed_cfg['channel_id']}",
        rel="alternate",
    )

    meeting_dirs = sorted(
        [d for d in feed_dir.iterdir() if d.is_dir()], reverse=True
    )
    for meeting_dir in meeting_dirs:
        metadata_path = meeting_dir / "metadata.json"
        if not metadata_path.exists():
            continue
        metadata = json.loads(metadata_path.read_text())
        if metadata.get("status") == "ignored_too_old":
            continue

        for story_file in sorted(meeting_dir.glob("[0-9]*.md")):
            story = parse_story_file(story_file)
            feed_entry = feed.add_entry()
            feed_entry.id(story["content_hash"])
            feed_entry.title(f"{story['title']} | {metadata.get('video_title', 'Video')}")
            feed_entry.link(
                href=f"https://youtu.be/{metadata['video_id']}?t={story['start_seconds']}"
            )
            yt_url = f"https://youtu.be/{metadata['video_id']}?t={story['start_seconds']}"
            video_title = metadata.get("video_title", "")
            feed_entry.content(
                f"<h2>{story['title']}</h2>"
                f"<p><strong>{story['dateline']}</strong></p>"
                f"<p><em>{video_title}</em> &mdash; "
                f"&#9654; <a href=\"{yt_url}\">{yt_url}</a></p>"
                f"<br>{story['body_html']}",
                type="html",
            )
            feed_entry.published(
                datetime.fromtimestamp(metadata["processed_at"]).astimezone()
            )

    feed.rss_file(feed_dir / "rss.xml", pretty=True)
    (feed_dir / "channel.json").write_text(
        json.dumps({"channel_id": feed_cfg["channel_id"], "channel_name": feed_cfg["channel_name"]})
    )


def rebuild_meta_feed(base_url: str = "") -> None:
    """Aggregate stories from all channel folders into ``archive/rss.xml``.

    Collects all stories across every channel sub-directory,
    ordered by processing timestamp (newest first).  Each entry is prefixed
    with ``[Channel Name]`` in the title so readers know the source.

    Args:
        base_url: Public URL of this feed (used as the RSS ``<self>`` link).
                  Omit or pass an empty string to leave the self-link out.
    """
    logger.info("TubeNews: Rebuilding meta-feed (archive/rss.xml)")

    feed = FeedGenerator()
    feed.id("tubenews_meta_rss")
    feed.title("TubeNews: News Feed")
    feed.description("Aggregated news from monitored channels.")
    # feedgen requires at least one link; fall back to YouTube if no base_url.
    feed.link(href=base_url if base_url else "https://www.youtube.com", rel="alternate")
    if base_url:
        feed.link(href=base_url, rel="self")

    all_stories: list[dict] = []
    for channel_dir in [d for d in STORAGE_ROOT.iterdir() if d.is_dir()]:
        for meeting_dir in [d for d in channel_dir.iterdir() if d.is_dir()]:
            metadata_path = meeting_dir / "metadata.json"
            if not metadata_path.exists():
                continue
            try:
                metadata = json.loads(metadata_path.read_text())
                if metadata.get("status") == "ignored_too_old":
                    continue
                for story_file in meeting_dir.glob("[0-9]*.md"):
                    all_stories.append({
                        "file": story_file,
                        "meta": metadata,
                        "channel_name": channel_dir.name.replace("_", " "),
                    })
            except Exception:
                continue

    all_stories.sort(key=lambda entry: entry["meta"].get("processed_at", 0), reverse=True)

    for entry in all_stories:
        try:
            story = parse_story_file(entry["file"])
            feed_entry = feed.add_entry()
            feed_entry.id(story["content_hash"])
            feed_entry.title(f"[{entry['channel_name']}] {story['title']}")
            feed_entry.link(
                href=f"https://youtu.be/{entry['meta']['video_id']}?t={story['start_seconds']}"
            )
            yt_url = f"https://youtu.be/{entry['meta']['video_id']}?t={story['start_seconds']}"
            video_title = entry["meta"].get("video_title", "")
            feed_entry.content(
                f"<h2>{story['title']}</h2>"
                f"<p><strong>{story['dateline']}</strong></p>"
                f"<p><em>{entry['channel_name']}: {video_title}</em> &mdash; "
                f"&#9654; <a href=\"{yt_url}\">{yt_url}</a></p>"
                f"<br>{story['body_html']}",
                type="html",
            )
            feed_entry.published(
                datetime.fromtimestamp(
                    entry["meta"].get("processed_at", time.time())
                ).astimezone()
            )
        except Exception:
            continue

    feed.rss_file(STORAGE_ROOT / "rss.xml", pretty=True)


def rebuild_user_feed(user: dict, base_url: str = "", user_id: str = "") -> None:
    """Generate ``archive/users/<id>/rss.xml`` filtered to a user's subscribed channels.

    Reads ``channel.json`` from each channel directory (written by :func:`rebuild_feed`)
    to determine which archive folders correspond to the user's ``channel_ids``.  Stories
    are sorted newest-first, matching :func:`rebuild_meta_feed` behaviour.

    Args:
        user:     User config dict from ``archive/users/<id>/user.json``.
                  Must contain ``name`` (str) and ``channel_ids`` (list[str]).
        base_url: Public URL root; currently unused but reserved for future self-links.
        user_id:  UUID directory name for the user. Falls back to slugify(name) if omitted.
    """
    name = user["name"]
    subscribed = set(user.get("channel_ids", []))
    user_dir = STORAGE_ROOT / "users" / (user_id or slugify(name))
    user_dir.mkdir(parents=True, exist_ok=True)

    logger.info(f"TubeNews: Rebuilding user feed for {name}")

    feed = FeedGenerator()
    feed.id(f"tubenews_user_{slugify(name)}")
    feed.title(f"TubeNews: {name}")
    feed.description("Personalized TubeNews feed.")
    feed.link(href=base_url if base_url else "https://www.youtube.com", rel="alternate")

    all_stories: list[dict] = []
    for channel_dir in [d for d in STORAGE_ROOT.iterdir() if d.is_dir() and d.name != "users"]:
        channel_json = channel_dir / "channel.json"
        if not channel_json.exists():
            continue
        try:
            channel_info = json.loads(channel_json.read_text())
        except Exception:
            continue
        if channel_info.get("channel_id") not in subscribed:
            continue
        channel_name = channel_info.get("channel_name", channel_dir.name.replace("_", " "))
        for meeting_dir in [d for d in channel_dir.iterdir() if d.is_dir()]:
            metadata_path = meeting_dir / "metadata.json"
            if not metadata_path.exists():
                continue
            try:
                metadata = json.loads(metadata_path.read_text())
                if metadata.get("status") == "ignored_too_old":
                    continue
                for story_file in meeting_dir.glob("[0-9]*.md"):
                    all_stories.append({"file": story_file, "meta": metadata, "channel_name": channel_name})
            except Exception:
                continue

    all_stories.sort(key=lambda entry: entry["meta"].get("processed_at", 0), reverse=True)

    for entry in all_stories:
        try:
            story = parse_story_file(entry["file"])
            feed_entry = feed.add_entry()
            feed_entry.id(story["content_hash"])
            feed_entry.title(f"[{entry['channel_name']}] {story['title']}")
            feed_entry.link(
                href=f"https://youtu.be/{entry['meta']['video_id']}?t={story['start_seconds']}"
            )
            yt_url = f"https://youtu.be/{entry['meta']['video_id']}?t={story['start_seconds']}"
            video_title = entry["meta"].get("video_title", "")
            feed_entry.content(
                f"<h2>{story['title']}</h2>"
                f"<p><strong>{story['dateline']}</strong></p>"
                f"<p><em>{entry['channel_name']}: {video_title}</em> &mdash; "
                f"&#9654; <a href=\"{yt_url}\">{yt_url}</a></p>"
                f"<br>{story['body_html']}",
                type="html",
            )
            feed_entry.published(
                datetime.fromtimestamp(
                    entry["meta"].get("processed_at", time.time())
                ).astimezone()
            )
        except Exception:
            continue

    feed.rss_file(user_dir / "rss.xml", pretty=True)


def rebuild_user_blog(user: dict, base_url: str = "", blog_days: int = 90, user_id: str = "") -> None:
    """Generate ``archive/users/<id>/index.html`` — a static blog page for a user.

    Pulls stories from the user's subscribed channels (same logic as
    :func:`rebuild_user_feed`) and renders them as a self-contained HTML page
    sorted newest-first.  Only stories processed within the last *blog_days* days
    are included so the page stays a manageable size.

    Args:
        user:      User config dict from ``archive/users/<id>/user.json``.
        base_url:  Public URL root; used to build a self-link in the page header.
        blog_days: How many days of stories to include (default 90).
        user_id:   UUID directory name for the user. Falls back to slugify(name) if omitted.
    """
    name = user["name"]
    subscribed = set(user.get("channel_ids", []))
    user_dir = STORAGE_ROOT / "users" / (user_id or slugify(name))
    user_dir.mkdir(parents=True, exist_ok=True)

    logger.info(f"TubeNews: Rebuilding blog for {name}")

    cutoff = time.time() - blog_days * 86400

    all_stories: list[dict] = []
    for channel_dir in [d for d in STORAGE_ROOT.iterdir() if d.is_dir() and d.name != "users"]:
        channel_json = channel_dir / "channel.json"
        if not channel_json.exists():
            continue
        try:
            channel_info = json.loads(channel_json.read_text())
        except Exception:
            continue
        if channel_info.get("channel_id") not in subscribed:
            continue
        channel_name = channel_info.get("channel_name", channel_dir.name.replace("_", " "))
        for meeting_dir in [d for d in channel_dir.iterdir() if d.is_dir()]:
            metadata_path = meeting_dir / "metadata.json"
            if not metadata_path.exists():
                continue
            try:
                metadata = json.loads(metadata_path.read_text())
                if metadata.get("status") == "ignored_too_old":
                    continue
                if metadata.get("processed_at", 0) < cutoff:
                    continue
                for story_file in meeting_dir.glob("[0-9]*.md"):
                    all_stories.append({"file": story_file, "meta": metadata, "channel_name": channel_name,
                                        "channel_slug": channel_dir.name, "meeting_id": meeting_dir.name})
            except Exception:
                continue

    all_stories.sort(key=lambda entry: entry["meta"].get("processed_at", 0), reverse=True)

    CSS = """
        body { font-family: Georgia, serif; margin: 0; padding: 0;
               color: #222; background: #fafaf8; }
        .blog-content { max-width: 740px; margin: 0 auto; padding: 30px 20px 40px; }
        h1 { font-size: 1.6em; border-bottom: 2px solid #333; padding-bottom: 8px; }
        .meta { font-size: 0.85em; color: #666; margin-bottom: 28px; }
        article { border-bottom: 1px solid #ddd; padding: 24px 0; }
        article:last-child { border-bottom: none; }
        h2 { font-size: 1.25em; margin: 0 0 4px 0; }
        .dateline { font-style: italic; color: #555; font-size: 0.9em; margin: 0 0 8px 0; }
        .source { font-size: 0.82em; color: #777; margin-bottom: 10px; }
        .source a { color: #555; }
        .body p { margin: 6px 0; line-height: 1.65; }
        .watch { display: inline-block; margin-top: 10px; font-size: 0.85em;
                 color: #c00; text-decoration: none; }
        .watch:hover { text-decoration: underline; }
    """

    story_blocks = []
    for entry in all_stories:
        try:
            story = parse_story_file(entry["file"])
        except Exception:
            continue
        yt_url = f"https://youtu.be/{entry['meta']['video_id']}?t={story['start_seconds']}"
        video_title = entry["meta"].get("video_title", "")
        # Convert body_html (br-separated) to proper paragraphs
        paras = "".join(
            f"<p>{p.strip()}</p>"
            for p in story["body_html"].split("<br>")
            if p.strip()
        )
        transcript_link = ""
        if entry.get("channel_slug") and entry.get("meeting_id"):
            t_url = f"/transcript/{entry['channel_slug']}/{entry['meeting_id']}#t{story['start_seconds']}"
            transcript_link = f" &mdash; <a class='watch' href='{t_url}' target='_blank' rel='noopener'>&#128221; Read transcript</a>"
        story_blocks.append(
            f"<article>\n"
            f"  <h2>{story['title']}</h2>\n"
            f"  <p class='dateline'>{story['dateline']}</p>\n"
            f"  <p class='source'>{entry['channel_name']}"
            + (f" &mdash; <em>{video_title}</em>" if video_title else "")
            + f" &mdash; <a class='watch' href='{yt_url}' target='_blank' rel='noopener'>&#9654; Watch source</a>"
            + transcript_link
            + f"</p>\n"
            f"  <div class='body'>{paras}</div>\n"
            f"</article>"
        )

    page_title = user.get("blog_name") or f"TubeNews — {name}"
    meta_line = (
        f"{len(all_stories)} stories from {len(subscribed)} channel{'s' if len(subscribed) != 1 else ''} "
        f"— last {blog_days} days"
    )
    rss_feed_path = f"/feed/{user['feed_token']}.xml"
    rss_link = f'<link rel="alternate" type="application/rss+xml" title="{page_title}" href="{rss_feed_path}">'

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{page_title}</title>
{rss_link}
<style>
        nav.blog-nav {{
            background: #fff; border-bottom: 1px solid #d1d5db;
            padding: 0 1.5rem; height: 52px;
            display: flex; align-items: center; justify-content: space-between;
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
        }}
        nav.blog-nav .nav-left {{ display: flex; align-items: center; gap: 1.5rem; }}
        nav.blog-nav a {{ color: #2563eb; text-decoration: none; font-size: 0.9rem; }}
        nav.blog-nav a:hover {{ text-decoration: underline; }}
        nav.blog-nav .nav-brand {{ font-weight: 700; font-size: 1.1rem; }}
        nav.blog-nav .nav-rss {{ display: flex; align-items: center; }}
        {CSS}
</style>
</head>
<body>
<nav class="blog-nav">
  <div class="nav-left">
    <a href="/" class="nav-brand">TubeNews</a>
    <a href="/dashboard">My feed</a>
  </div>
  <a href="{rss_feed_path}" class="nav-rss" title="Subscribe via RSS">
    <svg xmlns="http://www.w3.org/2000/svg" width="18" height="18" viewBox="0 0 16 16" aria-hidden="true">
      <circle cx="3" cy="13" r="2" fill="#f26522"/>
      <path fill="#f26522" d="M1 5a8 8 0 0 1 8 8H7a6 6 0 0 0-6-6V5z"/>
      <path fill="#f26522" d="M1 1a12 12 0 0 1 12 12h-2A10 10 0 0 0 1 3V1z"/>
    </svg>
  </a>
</nav>
<div class="blog-content">
<h1>{page_title}</h1>
<p class="meta">{meta_line}</p>
{"".join(story_blocks) if story_blocks else "<p>No stories yet.</p>"}
</div>
</body>
</html>
"""

    (user_dir / "index.html").write_text(html, encoding="utf-8")


# ---------------------------------------------------------------------------
# Per-video / per-feed processing
# ---------------------------------------------------------------------------


def mark_video_as_backlog(feed_dir: Path, video_id: str) -> None:
    """Create a stub archive folder marking *video_id* as a backlog item.

    When a channel is first added to the config, its entire back-catalogue
    would be processed on the next run.  To avoid that, we call this function
    for every video except the newest one, creating a dated stub that the main
    loop treats as already handled.

    The ``2000-01-01`` date prefix keeps these stubs sorted before real
    meetings in tab completion while staying clearly distinct from actual dates.
    """
    stub_dir = feed_dir / f"2000-01-01_{video_id}"
    stub_dir.mkdir(exist_ok=True)
    metadata = {
        "video_id": video_id,
        "status": "ignored_too_old",
        "processed_at": time.time(),
    }
    (stub_dir / "metadata.json").write_text(json.dumps(metadata))


def process_video(
    video_id: str,
    video_title: str,
    video_date: str,
    is_live: bool,
    feed: dict,
    feed_dir: Path,
    supadata_client: Supadata,
    config: dict,
    ai_disabled: bool,
    video_num: int = 0,
    total_videos: int = 0,
) -> str:
    """Fetch, analyse, and archive one video.

    Attempts to reuse a locally-cached transcript (from a previous run that
    completed the fetch but failed during AI analysis) before hitting the
    Supadata API again.

    Returns a ``(status, n_stories)`` tuple where *status* is one of:

        ``"content_written"``  – stories were generated and written to disk;
                                 *n_stories* is the count written.
        ``"ai_rate_limited"``  – Gemini returned 429; caller should set
                                 ``ai_disabled = True`` for the session;
                                 *n_stories* is 0.
        ``"skipped"``          – transcript unavailable, live stream, AI
                                 disabled, or AI returned nothing; *n_stories*
                                 is 0.
    """
    # Locate any pre-existing archive folder for this video ID.
    existing_dir = next(
        (d for d in feed_dir.iterdir() if d.is_dir() and d.name.endswith(video_id)),
        None,
    )

    channel_name = feed["channel_name"]

    # --- Load or fetch transcript ---
    if existing_dir and (existing_dir / "transcript.txt").exists():
        # Re-use cached transcript; only the AI step needs to re-run.
        logger.info(f"{channel_name}: {video_title}: TubeNews: Found cached transcript, re-running AI")
        transcript_text = (existing_dir / "transcript.txt").read_text(encoding="utf-8")
        video_date = existing_dir.name.split("_")[0]
        meeting_dir = existing_dir
    else:
        counter = f" ({video_num}/{total_videos})" if total_videos else ""
        logger.info(f"{channel_name}: {video_title}: TubeNews: Processing new video{counter}")
        if is_live:
            logger.info(f"{channel_name}: {video_title}: TubeNews: Live stream — skipping, will retry next run")
            return "skipped", 0
        logger.debug(f"{channel_name}: {video_title}: Supadata: Requesting transcript")
        transcript_text = fetch_transcript(
            video_id, supadata_client,
            feed_name=channel_name, video_title=video_title,
        )
        if not transcript_text:
            return "skipped", 0

        meeting_dir = feed_dir / f"{video_date}_{video_id}"
        meeting_dir.mkdir(exist_ok=True)
        (meeting_dir / "transcript.txt").write_text(transcript_text, encoding="utf-8")

    # --- Generate news stories via Gemini ---
    if ai_disabled:
        return "skipped", 0

    logger.info(f"{channel_name}: {video_title}: Gemini: Generating stories")
    stories = call_gemini_api(
        transcript_text=transcript_text,
        focus=feed["focus"],
        video_title=video_title,
        video_date=video_date,
        gemini_api_key=config["gemini_api_key"],
        model_name=config["gemini_model"],
        feed_name=channel_name,
    )

    if stories is False:
        return "ai_rate_limited", 0

    if stories:
        write_story_files(stories, meeting_dir, video_id)
        metadata = {
            "video_id": video_id,
            "video_title": video_title,
            "status": "processed",
            "processed_at": time.time(),
        }
        (meeting_dir / "metadata.json").write_text(json.dumps(metadata))
        return "content_written", len(stories)

    # Gemini returned an empty story list — write metadata so this video is
    # not re-submitted to the AI on future runs.
    metadata = {
        "video_id": video_id,
        "video_title": video_title,
        "status": "no_stories",
        "processed_at": time.time(),
    }
    (meeting_dir / "metadata.json").write_text(json.dumps(metadata))
    return "skipped", 0


def process_feed(
    feed: dict,
    supadata_client: Supadata,
    config: dict,
    ai_rate_limit_event: threading.Event | None = None,
) -> tuple[bool, bool]:
    """Process all new videos for one configured channel.

    Discovers videos from YouTube, skips ones already archived, and calls
    :func:`process_video` for each new one.

    *ai_rate_limit_event* is an optional shared :class:`threading.Event`.
    When set (by any channel hitting a 429), all channels skip further AI
    calls for the remainder of the run.  Pass ``None`` for single-channel use.

    Returns:
        A ``(content_changed, ai_rate_limited, stories_written)`` tuple.
        *content_changed* is True if any new stories were written (i.e., the
        RSS feed needs to be rebuilt).
        *ai_rate_limited* is True if Gemini hit its quota during this feed.
        *stories_written* is the total count of story files created.
    """
    channel_slug = slugify(feed["channel_name"])
    channel_name = feed["channel_name"]
    logger.info(f"{channel_name}: TubeNews: Starting feed check")
    feed_dir = STORAGE_ROOT / channel_slug

    # If the RSS file doesn't exist yet, treat content as changed so we always
    # build an initial feed even if nothing new was processed this run.
    content_changed = not (feed_dir / "rss.xml").exists()
    is_new_feed = not feed_dir.exists()
    ai_rate_limited = False
    stories_written = 0

    feed_dir.mkdir(parents=True, exist_ok=True)

    all_videos = discover_videos(feed["channel_id"], feed_name=channel_name)
    if not all_videos:
        return content_changed, ai_rate_limited

    all_ids = [v["id"] for v in all_videos]
    video_meta = {v["id"]: v for v in all_videos}

    # Videos whose archive folder doesn't exist yet.
    unprocessed = [
        v for v in all_videos
        if not any(
            d.name.endswith(v["id"])
            for d in feed_dir.iterdir()
            if d.is_dir() and (d / "metadata.json").exists()
        )
    ]

    if unprocessed:
        logger.info(f"{channel_name}: TubeNews: Found {len(unprocessed)} new video(s)")
    else:
        logger.info(f"{channel_name}: TubeNews: No new videos")

    # Hold same-day videos — YouTube's auto-caption pipeline needs time to
    # finish, and transcript proxies can return garbage for very fresh videos.
    today_str = date.today().isoformat()
    fresh = [
        v for v in unprocessed
        if v["date"] == today_str and not (is_new_feed and all_ids.index(v["id"]) > 0)
    ]
    if fresh:
        noun = "video" if len(fresh) == 1 else "videos"
        logger.info(f"{channel_name}: TubeNews: Holding {len(fresh)} {noun} posted today — will process tomorrow")

    if is_new_feed:
        backlog_count = len([v for v in unprocessed if all_ids.index(v["id"]) > 0])
        if backlog_count:
            logger.info(f"{channel_name}: TubeNews: New feed — marking {backlog_count} backlog video(s) as watched")

    # Videos that will actually be processed (not back-catalogued, not too fresh).
    videos_to_process = [
        v for v in unprocessed
        if not (is_new_feed and all_ids.index(v["id"]) > 0)
        and v["date"] != today_str
    ]
    total = len(videos_to_process)

    for video_info in unprocessed:
        # On a brand-new feed, skip the entire back-catalogue except the
        # most-recent video (index 0 in all_ids).  This prevents the
        # first run from processing months of old meetings.
        if is_new_feed and all_ids.index(video_info["id"]) > 0:
            mark_video_as_backlog(feed_dir, video_info["id"])
            content_changed = True
            continue

        if video_info["date"] == today_str:
            continue  # held until tomorrow's run

        video_num = videos_to_process.index(video_info) + 1
        ai_disabled = ai_rate_limited or (
            ai_rate_limit_event is not None and ai_rate_limit_event.is_set()
        )
        result, n = process_video(
            video_id=video_info["id"],
            video_title=video_info["title"],
            video_date=video_info["date"],
            is_live=video_info["is_live"],
            feed=feed,
            feed_dir=feed_dir,
            supadata_client=supadata_client,
            config=config,
            ai_disabled=ai_disabled,
            video_num=video_num,
            total_videos=total,
        )

        if result == "content_written":
            content_changed = True
            stories_written += n
        elif result == "ai_rate_limited":
            ai_rate_limited = True
            if ai_rate_limit_event is not None:
                ai_rate_limit_event.set()

    return content_changed, ai_rate_limited, stories_written


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    """Load config, process each feed, and rebuild RSS outputs."""
    parser = argparse.ArgumentParser(description="TubeNews — YouTube channel monitor")
    parser.add_argument("--debug", action="store_true", help="Enable verbose debug output")
    args = parser.parse_args()

    setup_logging(args.debug)

    with open(CONFIG_FILE, "r") as config_file:
        config = json.load(config_file)

    supadata_client = Supadata(api_key=config["supadata_api_key"])
    logger.info(f"Session Start | {datetime.now().strftime('%A, %B %-d, %Y')} | AI Model: {config.get('gemini_model')}")

    ai_rate_limit_event = threading.Event()
    any_content_changed = threading.Event()

    def _run_feed(feed: dict) -> dict:
        content_changed, _, stories_written = process_feed(
            feed, supadata_client, config, ai_rate_limit_event
        )
        if content_changed:
            rebuild_feed(STORAGE_ROOT / slugify(feed["channel_name"]), feed)
            any_content_changed.set()
        return {
            "channel_id": feed["channel_id"],
            "channel_name": feed["channel_name"],
            "stories_written": stories_written,
        }

    started_at = time.time()
    max_workers = min(len(config["feeds"]), config.get("max_parallel_feeds", 3))
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        feed_results = list(executor.map(_run_feed, config["feeds"]))
    total_stories = sum(r["stories_written"] for r in feed_results)

    if any_content_changed.is_set() or not (STORAGE_ROOT / "rss.xml").exists():
        rebuild_meta_feed(base_url=config.get("base_url", ""))

    users_dir = STORAGE_ROOT / "users"
    if users_dir.is_dir():
        for user_json in sorted(users_dir.glob("*/user.json")):
            user = json.loads(user_json.read_text())
            uid = user_json.parent.name
            rebuild_user_feed(user, base_url=config.get("base_url", ""), user_id=uid)

    story_word = "story" if total_stories == 1 else "stories"
    logger.info(f"Session End. {total_stories} new {story_word} published.")

    run_log_path = STORAGE_ROOT / "run_log.json"
    try:
        runs = json.loads(run_log_path.read_text()) if run_log_path.exists() else []
    except Exception:
        runs = []
    runs.append({
        "started_at": started_at,
        "finished_at": time.time(),
        "total_stories": total_stories,
        "ai_rate_limited": ai_rate_limit_event.is_set(),
        "feeds": feed_results,
    })
    try:
        run_log_path.write_text(json.dumps(runs[-30:], indent=2))
    except Exception:
        pass


if __name__ == "__main__":
    main()
