"""TubeNews — monitor YouTube channels for government meeting videos.

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
import hashlib
import json
import logging
import os
import re
import socket
import time
from datetime import datetime
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
    body_html = "<br>".join(lines[2:]).replace("\n", "<br>")

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


def discover_video_ids(channel_id: str) -> list[str]:
    """Scrape a channel's *videos* and *streams* tabs; return unique video IDs.

    YouTube embeds a JSON blob inside the page HTML that contains ``videoId``
    fields for every visible video.  We extract those with a regex rather than
    relying on an API key.

    Returns an ordered list of IDs (most-recent first, duplicates removed).
    """
    all_ids: list[str] = []
    for tab in ["videos", "streams"]:
        url = f"https://www.youtube.com/channel/{channel_id}/{tab}"
        logger.debug(f"Discovery: fetching YouTube tab '{tab}'…")
        try:
            response = requests.get(url, headers=YOUTUBE_HEADERS, timeout=10)
            if response.status_code == 200:
                found = re.findall(r'"videoId":"([^"]{11})"', response.text)
                if not found:
                    # YouTube occasionally changes its HTML structure; warn so
                    # the operator knows the scraping regex may need updating.
                    logger.warning(
                        f"Discovery: got 200 from '{tab}' tab but found 0 "
                        "video IDs — YouTube HTML structure may have changed."
                    )
                all_ids.extend(found)
        except Exception as exc:
            logger.debug(f"Discovery: tab '{tab}' failed: {exc}")

    # dict.fromkeys preserves insertion order while removing duplicates.
    return list(dict.fromkeys(all_ids))


def scrape_youtube_metadata(video_id: str) -> tuple[str, str]:
    """Scrape the YouTube watch page for a video's title and upload date.

    YouTube embeds structured data (JSON-LD) directly in the page HTML, which
    contains both ``uploadDate`` and the ``<title>`` tag.  No API key needed.

    Returns:
        A ``(upload_date, video_title)`` tuple.
        *upload_date* is ``"YYYY-MM-DD"`` or today's date if scraping fails.
        *video_title* is the human-readable title or *video_id* as a fallback.
    """
    url = f"https://www.youtube.com/watch?v={video_id}"

    # Safe fallbacks used when the page can't be fetched or parsed.
    upload_date = datetime.now().strftime("%Y-%m-%d")
    video_title = video_id

    try:
        response = requests.get(url, headers=YOUTUBE_HEADERS, timeout=10)
        if response.status_code == 200:
            date_match = re.search(
                r'"uploadDate":"(\d{4}-\d{2}-\d{2})"', response.text
            )
            if date_match:
                upload_date = date_match.group(1)

            title_match = re.search(
                r"<title>(.+?) - YouTube</title>", response.text
            )
            if title_match:
                video_title = title_match.group(1)
    except Exception as exc:
        logger.debug(f"YouTube page scrape failed for {video_id}: {exc}")

    return upload_date, video_title


def fetch_transcript(video_id: str, supadata_client: Supadata) -> str | None:
    """Fetch timed transcript segments from the Supadata API.

    Each segment is formatted as ``"<offset_seconds>s --> <text>"`` so Gemini
    knows where each sentence occurs in the video timeline.

    Returns the formatted transcript string, or None if the API call fails.
    """
    url = f"https://www.youtube.com/watch?v={video_id}"
    logger.debug(f"Supadata: requesting transcript for {video_id}…")

    try:
        transcript_response = supadata_client.transcript(url=url, text=False)
        if hasattr(transcript_response, "content") and transcript_response.content:
            segments = transcript_response.content
            lines = [
                f"{int(getattr(seg, 'offset', 0) / 1000)}s --> {getattr(seg, 'text', '')}"
                for seg in segments
            ]
            logger.debug(f"Supadata: received {len(segments)} segments.")
            return "\n".join(lines)
    except Exception as exc:
        logger.error(f"Supadata call failed: {exc}")

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
        f"You are a highly experienced investigative reporter specializing in "
        f"local government. Analyze this transcript of '{video_title}' held on "
        f"{video_date}.\n\n"
        f"OBJECTIVE: Identify and extract distinct news stories strictly "
        f"relevant to this FOCUS: '{focus}'.\n\n"
        "WRITING GUIDELINES:\n"
        "1. TONE: Professional, objective, and authoritative. Use the Inverted "
        "Pyramid style.\n"
        "2. CONTENT: Focus on the 'Why it Matters'. Skip ceremonial talk.\n"
        "3. DATELINE: Construct a formal AP-style dateline "
        "(e.g., 'GONZALES, Calif. — March 14, 2026').\n\n"
        "Return result ONLY as raw JSON list of objects with keys: "
        "'title', 'dateline', 'content', 'start_time_seconds'."
    )

    payload = {
        "contents": [
            {"parts": [{"text": f"{directive}\n\nTRANSCRIPT:\n{transcript_text}"}]}
        ]
    }

    try:
        response = requests.post(api_url, json=payload, timeout=150)
        if response.status_code == 200:
            raw_text = response.json()["candidates"][0]["content"]["parts"][0]["text"]
            json_match = re.search(r"\[\s*{.*}\s*\]", raw_text, re.DOTALL)
            stories = json.loads(json_match.group(0) if json_match else raw_text)
            logger.debug(f"Gemini: generated {len(stories)} stories.")
            return stories
        elif response.status_code == 429:
            logger.warning("Gemini rate limit hit — AI disabled for this run.")
            return False
    except Exception as exc:
        logger.error(f"AI call failed: {exc}")

    return None


def write_story_files(stories: list, meeting_dir: Path) -> None:
    """Write each story dict as a numbered Markdown file inside *meeting_dir*.

    File names are ``01_<slug>.md``, ``02_<slug>.md``, …  Any stale story
    files from a previous (failed) run are deleted first to avoid mixing
    results from different Gemini calls.

    File format::

        # Story Title
        *AP-style dateline*

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
            fh.write(f"*{story.get('dateline', 'California')}*\n\n")
            fh.write(f"{story['content']}\n\n")
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
    logger.info(f"--> Step 4: Rebuilding RSS for {safe_name}")

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
            feed_entry.title(f"{story['title']} | {metadata.get('video_title', 'Meeting')}")
            feed_entry.link(
                href=f"https://youtu.be/{metadata['video_id']}?t={story['start_seconds']}"
            )
            feed_entry.content(
                f"<strong>{story['dateline']}</strong><br><br>{story['body_html']}",
                type="html",
            )
            feed_entry.published(
                datetime.fromtimestamp(metadata["processed_at"]).astimezone()
            )

    feed.rss_file(feed_dir / "rss.xml", pretty=True)


def rebuild_meta_feed(base_url: str = "") -> None:
    """Aggregate stories from all channel folders into ``archive/rss.xml``.

    Collects all stories across every channel sub-directory,
    ordered by processing timestamp (newest first).  Each entry is prefixed
    with ``[Channel Name]`` in the title so readers know the source.

    Args:
        base_url: Public URL of this feed (used as the RSS ``<self>`` link).
                  Omit or pass an empty string to leave the self-link out.
    """
    logger.info("--> Step 5: Rebuilding Regional Meta-Feed (archive/rss.xml)…")

    feed = FeedGenerator()
    feed.id("tubenews_meta_rss")
    feed.title("TubeNews: Regional Real Estate & Development")
    feed.description("Aggregated regional reporting.")
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
            feed_entry.content(
                f"<strong>{story['dateline']}</strong>"
                f"<br><em>Source: {entry['channel_name']}</em>"
                f"<br><br>{story['body_html']}",
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
    feed: dict,
    feed_dir: Path,
    supadata_client: Supadata,
    config: dict,
    ai_disabled: bool,
) -> str:
    """Fetch, analyse, and archive one video.

    Attempts to reuse a locally-cached transcript (from a previous run that
    completed the fetch but failed during AI analysis) before hitting the
    Supadata API again.

    Returns one of:
        ``"content_written"``  – stories were generated and written to disk.
        ``"ai_rate_limited"``  – Gemini returned 429; caller should set
                                 ``ai_disabled = True`` for the session.
        ``"skipped"``          – transcript unavailable or AI returned nothing.
    """
    # Locate any pre-existing archive folder for this video ID.
    existing_dir = next(
        (d for d in feed_dir.iterdir() if d.is_dir() and d.name.endswith(video_id)),
        None,
    )

    # --- Load or fetch transcript ---
    if existing_dir and (existing_dir / "transcript.txt").exists():
        # Re-use cached transcript; only the AI step needs to re-run.
        logger.info(f"[✓] Found local transcript for {video_id}. Re-running AI only.")
        transcript_text = (existing_dir / "transcript.txt").read_text(encoding="utf-8")
        video_date = existing_dir.name.split("_")[0]
        video_title = video_id          # title isn't cached; use ID as fallback
        meeting_dir = existing_dir
    else:
        logger.info(f"[+] Processing new video: {video_id}")
        logger.info("--> Step 2: Requesting transcript + metadata from Supadata…")

        video_date, video_title = scrape_youtube_metadata(video_id)
        transcript_text = fetch_transcript(video_id, supadata_client)
        if not transcript_text:
            return "skipped"

        meeting_dir = feed_dir / f"{video_date}_{video_id}"
        meeting_dir.mkdir(exist_ok=True)
        (meeting_dir / "transcript.txt").write_text(transcript_text, encoding="utf-8")

    # --- Generate news stories via Gemini ---
    if ai_disabled:
        return "skipped"

    logger.info(f"--> Step 3: AI Analysis via {config['gemini_model']}…")
    stories = call_gemini_api(
        transcript_text=transcript_text,
        focus=feed["focus"],
        video_title=video_title,
        video_date=video_date,
        gemini_api_key=config["gemini_api_key"],
        model_name=config["gemini_model"],
    )

    if stories is False:
        return "ai_rate_limited"

    if stories:
        write_story_files(stories, meeting_dir)
        metadata = {
            "video_id": video_id,
            "video_title": video_title,
            "status": "processed",
            "processed_at": time.time(),
        }
        (meeting_dir / "metadata.json").write_text(json.dumps(metadata))
        return "content_written"

    return "skipped"


def process_feed(
    feed: dict,
    supadata_client: Supadata,
    config: dict,
) -> tuple[bool, bool]:
    """Process all new videos for one configured channel.

    Discovers videos from YouTube, skips ones already archived, and calls
    :func:`process_video` for each new one.

    Returns:
        A ``(content_changed, ai_rate_limited)`` tuple.
        *content_changed* is True if any new stories were written (i.e., the
        RSS feed needs to be rebuilt).
        *ai_rate_limited* is True if Gemini hit its quota so the caller can
        disable AI for subsequent feeds in the same run.
    """
    channel_slug = slugify(feed["channel_name"])
    logger.info(f"[*] Feed: {feed['channel_name']}")
    feed_dir = STORAGE_ROOT / channel_slug

    # If the RSS file doesn't exist yet, treat content as changed so we always
    # build an initial feed even if nothing new was processed this run.
    content_changed = not (feed_dir / "rss.xml").exists()
    is_new_feed = not feed_dir.exists()
    ai_rate_limited = False

    feed_dir.mkdir(parents=True, exist_ok=True)

    all_video_ids = discover_video_ids(feed["channel_id"])
    if not all_video_ids:
        return content_changed, ai_rate_limited

    # Videos whose archive folder doesn't exist yet.
    unprocessed_video_ids = [
        vid for vid in all_video_ids
        if not any(
            d.name.endswith(vid)
            for d in feed_dir.iterdir()
            if d.is_dir() and (d / "metadata.json").exists()
        )
    ]

    if unprocessed_video_ids:
        logger.info(f"--> Step 1: Found {len(unprocessed_video_ids)} videos to check.")
    else:
        logger.info("--> Step 1: No new videos discovered.")

    for video_id in unprocessed_video_ids:
        # On a brand-new feed, skip the entire back-catalogue except the
        # most-recent video (index 0 in all_video_ids).  This prevents the
        # first run from processing months of old meetings.
        if is_new_feed and all_video_ids.index(video_id) > 0:
            mark_video_as_backlog(feed_dir, video_id)
            content_changed = True
            continue

        result = process_video(
            video_id=video_id,
            feed=feed,
            feed_dir=feed_dir,
            supadata_client=supadata_client,
            config=config,
            ai_disabled=ai_rate_limited,
        )

        if result == "content_written":
            content_changed = True
        elif result == "ai_rate_limited":
            ai_rate_limited = True

    return content_changed, ai_rate_limited


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    """Load config, process each feed, and rebuild RSS outputs."""
    parser = argparse.ArgumentParser(description="TubeNews — YouTube meeting monitor")
    parser.add_argument("--debug", action="store_true", help="Enable verbose debug output")
    args = parser.parse_args()

    setup_logging(args.debug)

    with open(CONFIG_FILE, "r") as config_file:
        config = json.load(config_file)

    supadata_client = Supadata(api_key=config["supadata_api_key"])
    logger.info(f"Session Start | AI Model: {config.get('gemini_model')}")

    any_content_changed = False
    ai_disabled = False

    for feed in config["feeds"]:
        content_changed, rate_limited = process_feed(feed, supadata_client, config)
        if rate_limited:
            ai_disabled = True
        if content_changed:
            rebuild_feed(STORAGE_ROOT / slugify(feed["channel_name"]), feed)
            any_content_changed = True

    if any_content_changed or not (STORAGE_ROOT / "rss.xml").exists():
        rebuild_meta_feed(base_url=config.get("base_url", ""))

    logger.info("Session End.")


if __name__ == "__main__":
    main()
