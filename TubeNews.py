#!/usr/bin/env python3
"""TubeNews — monitor YouTube channels for new videos and generate news feeds.

Workflow for each configured channel (feed):
  1. Discover recent video IDs by scraping YouTube channel pages.
  2. Skip videos already in the local archive.
  3. For genuinely new videos: fetch a transcript via the Supadata API and
     scrape basic metadata (title, upload date) from the YouTube watch page.
  4. Send the transcript to Google Gemini so it can extract focused news
     stories and write each story as a Markdown file.
  5. Rebuild the per-channel RSS feed and the site-wide aggregate feed.

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
import html
import json
import logging
import os
import re
import socket
import threading
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import TypedDict

# ---------------------------------------------------------------------------
# Third-party imports
# ---------------------------------------------------------------------------
import requests
from feedgen.feed import FeedGenerator
from supadata import Supadata, SupadataError

# ---------------------------------------------------------------------------
# Environment & paths
# ---------------------------------------------------------------------------

BASE_DIR = Path(__file__).resolve().parent
CONFIG_FILE = BASE_DIR / "TubeNews.json"

def _resolve_early_config(config_file: Path, base_dir: Path) -> tuple[Path, int]:
    """Read path/network settings from *config_file* before main() runs.

    Returns ``(STORAGE_ROOT, REQUEST_TIMEOUT)``.  All keys are optional;
    sensible defaults are returned when the file is absent or a key is missing.

    Args:
        config_file: Path to TubeNews.json.
        base_dir:    Directory used to resolve relative ``content_dir`` paths.
    """
    try:
        cfg = json.loads(config_file.read_text())

        # content_dir — where processed content is stored.
        # Absolute paths are used as-is; relative paths resolve from base_dir.
        content_dir = cfg.get("content_dir", "")
        if content_dir:
            p = Path(content_dir)
            storage_root = p if p.is_absolute() else (base_dir / p).resolve()
        else:
            storage_root = base_dir / "content"

        # request_timeout — seconds before giving up on YouTube / Supadata calls.
        request_timeout = int(cfg.get("request_timeout", 15))

    except Exception as exc:
        logging.warning(f"Failed to load config; using defaults: {exc}")
        storage_root = base_dir / "content"
        request_timeout = 15

    return storage_root, request_timeout


STORAGE_ROOT, REQUEST_TIMEOUT = _resolve_early_config(CONFIG_FILE, BASE_DIR)

# FreeBSD ships its CA bundle in a non-standard location; tell Python where
# to find it so HTTPS requests succeed. On Linux/macOS this path won't exist
# and the assignment is skipped.
_FREEBSD_CERT = "/usr/local/share/certs/ca-root-nss.crt"
if os.path.exists(_FREEBSD_CERT):
    os.environ["SSL_CERT_FILE"] = _FREEBSD_CERT

# Apply the timeout as the process-wide socket default so every network call
# (including Supadata's underlying HTTP) respects it automatically.
socket.setdefaulttimeout(REQUEST_TIMEOUT)

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
# Data contracts (TypedDicts)
# ---------------------------------------------------------------------------


class VideoInfo(TypedDict):
    """One discovered video entry from :func:`discover_videos`."""
    id: str
    title: str
    date: str
    is_live: bool


class FeedConfig(TypedDict):
    """Per-channel configuration block from ``TubeNews.json``."""
    channel_id: str
    channel_name: str
    focus: str


class GeminiStory(TypedDict):
    """One story dict as returned by :func:`call_gemini_api`."""
    title: str
    dateline: str
    content: str
    start_time_seconds: int
    topics: list[str]


class ParsedStory(TypedDict):
    """Structured fields extracted from a ``.md`` story file by :func:`parse_story_file`."""
    title: str
    dateline: str
    body_html: str
    start_seconds: int
    topics: list[str]
    content_hash: str
    user_ids: list[str]


class MetadataDict(TypedDict, total=False):
    """Contents of a ``metadata.json`` archive file.

    All fields are optional (``total=False``) because metadata files may be
    written incrementally and old files pre-date several keys.
    """
    video_id: str
    video_title: str
    status: str
    processed_at: float
    processed_focuses: list[str]


class FeedResult(TypedDict):
    """Per-channel result dict collected by ``_main_body``."""
    channel_id: str
    channel_name: str
    stories_written: int


# ---------------------------------------------------------------------------
# Utility helpers
# ---------------------------------------------------------------------------


from tubenews_utils import slugify  # noqa: E402  (below module-level constants)


def _fmt_no_leading_zeros(dt: datetime, fmt: str) -> str:
    """Format *dt* with *fmt* and strip leading zeros from day/hour fields.

    Replaces the POSIX-only ``%-d``/``%-I`` strftime codes with a portable
    alternative.  Only zeros preceded by a space are removed, so two-digit
    numbers such as 10, 11, 12 are unaffected.

    Example::
        >>> from datetime import datetime
        >>> _fmt_no_leading_zeros(datetime(2026, 1, 5, 9, 30), "%B %d, %Y at %I:%M %p")
        'January 5, 2026 at 9:30 AM'
    """
    return re.sub(r" 0(\d)", r" \1", dt.strftime(fmt))


def parse_story_file(story_path: Path) -> ParsedStory:
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
        if l.strip() != "---" and not l.startswith("**Segment Start:**") and not l.startswith("**Source:**") and not l.startswith("**Topics:**") and not l.startswith("**Users:**")
    ]
    body_html = "<br>".join(html.escape(l) for l in body_lines)

    timestamp_match = re.search(r"\*\*Segment Start:\*\* (\d+)s", text)
    start_seconds = int(timestamp_match.group(1)) if timestamp_match else 0

    topics_match = re.search(r"\*\*Topics:\*\*\s*(.+)", text)
    topics = (
        [t.strip() for t in topics_match.group(1).split(",") if t.strip()]
        if topics_match else []
    )

    users_match = re.search(r"\*\*Users:\*\*\s*(.+)", text)
    user_ids = (
        [u.strip() for u in users_match.group(1).split(",") if u.strip()]
        if users_match else []
    )

    return {
        "title": title,
        "dateline": dateline,
        "body_html": body_html,
        "start_seconds": start_seconds,
        "topics": topics,
        "user_ids": user_ids,
        # Keep a hash of the raw text so feed entry IDs are stable across runs.
        "content_hash": hashlib.md5(text.encode()).hexdigest(),
    }


def _story_matches_focus(story_topics: list[str], focuses: list[str]) -> bool:
    """Return True if a story should be shown for the given focus configuration.

    *focuses* is a list of focus strings (one per user-configured focus line).
    A story passes if it matches **any** of the focus strings.

    Matching rules:
    - No focus set (empty list) → always True (show everything).
    - Story has no topics (written before topic tagging was added) → always True
      (graceful degradation; don't hide old content).
    - Otherwise: any story topic that is a substring of a focus keyword, or vice
      versa, counts as a match.  This handles plurals and compound phrases
      (e.g. topic ``"housing"`` matches focus ``"affordable housing"``).

    Examples:
        >>> _story_matches_focus(["housing", "zoning"], ["housing, permits"])
        True
        >>> _story_matches_focus(["contracts"], ["housing, zoning"])
        False
        >>> _story_matches_focus([], ["housing"])
        True
        >>> _story_matches_focus(["budget"], [])
        True
        >>> _story_matches_focus(["roads"], ["housing", "transportation, roads"])
        True
    """
    focuses = [f for f in (focuses or []) if f and f.strip()]
    if not focuses:
        return True
    if not story_topics:
        return True
    for focus_str in focuses:
        focus_words = [w.strip().lower() for w in focus_str.split(",") if w.strip()]
        for topic in story_topics:
            t = topic.lower()
            for fw in focus_words:
                if t in fw or fw in t:
                    return True
    return False


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
        for fmt in ("%b %d, %Y", "%B %d, %Y"):
            try:
                return datetime.strptime(exact.group(1), fmt).strftime("%Y-%m-%d")
            except ValueError:
                continue

    # "N seconds/minutes/hours/days/weeks/months/years ago"
    m = re.search(r"(\d+)\s+(second|minute|hour|day|week|month|year)s?\s+ago", lower)
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
                title = runs[0].get("text", "") if runs else obj["title"].get("simpleText", "")

                relative = obj.get("publishedTimeText", {}).get("simpleText", "")
                date = _relative_date_to_iso(relative) if relative else datetime.now().strftime("%Y-%m-%d")
                logger.debug(f"  video {vid}: publishedTimeText={relative!r} → date={date}")

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


def discover_videos(channel_id: str, feed_name: str = "") -> list[VideoInfo]:
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
                response = requests.get(url, headers=YOUTUBE_HEADERS, timeout=REQUEST_TIMEOUT)
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
        tab_meta = _parse_channel_page_metadata(html)
        found = re.findall(r'"videoId":"([^"]{11})"', html)
        if not found:
            logger.warning(
                f"{prefix}YouTube: Got 200 from {tab} tab but found 0 "
                "video IDs — YouTube HTML structure may have changed."
            )
        elif not tab_meta:
            logger.warning(
                f"{prefix}YouTube: Found {len(found)} video ID(s) on {tab} tab "
                "but could not parse any titles or dates — "
                "YouTube HTML structure may have changed "
                f"(run: python3 helpers/dump_channel_html.py {channel_id})."
            )
        meta_lookup.update(tab_meta)
        all_ids.extend(found)

    today = datetime.now().strftime("%Y-%m-%d")
    seen: dict[str, dict] = {}
    for vid in all_ids:
        if vid not in seen:
            m = meta_lookup.get(vid, {})
            seen[vid] = {
                "id": vid,
                "title": m.get("title") or "",
                "date": m.get("date") or today,
                "is_live": m.get("is_live", False),
            }
    return list(seen.values())



def fetch_transcript(
    video_id: str,
    supadata_client: Supadata,
    feed_name: str = "",
    video_title: str = "",
    transcript_rate_limit_event: threading.Event | None = None,
) -> str | None | bool:
    """Fetch timed transcript segments from the Supadata API.

    Each segment is formatted as ``"<offset_seconds>s --> <text>"`` so Gemini
    knows where each sentence occurs in the video timeline.

    When a quota-exhausted error is detected (HTTP 402 or a
    ``SupadataError`` whose ``error`` code suggests credit exhaustion),
    *transcript_rate_limit_event* is set so that ``process_video`` and
    ``process_feed`` can abort remaining videos immediately.

    Returns:
        str  — formatted transcript on success.
        None — transient failure (network error, rate limit, etc.); will retry next run.
        False — permanent no-transcript (Supadata confirmed the video has no captions);
                caller should write ``status: "no_transcript_available"`` and stop retrying.
    """
    prefix = ": ".join(p for p in [feed_name, video_title] if p)
    prefix = f"{prefix}: " if prefix else ""

    url = f"https://www.youtube.com/watch?v={video_id}"
    try:
        transcript_response = supadata_client.transcript(url=url, lang="en", text=False)
        if hasattr(transcript_response, "content") and transcript_response.content:
            segments = transcript_response.content
            lang_received = getattr(transcript_response, "lang", "") or ""
            if lang_received and lang_received != "en":
                logger.warning(f"{prefix}Supadata: Requested English transcript but received '{lang_received}'")
            else:
                logger.debug(f"{prefix}Supadata: Language: {lang_received or 'unknown'}")
            lines = [
                f"{int(getattr(seg, 'offset', 0) / 1000)}s --> {getattr(seg, 'text', '')}"
                for seg in segments
            ]
            transcript_text = "\n".join(lines)
            logger.info(f"{prefix}Supadata: Transcript ready — {len(segments)} segments, {len(transcript_text):,} chars")
            return transcript_text
        else:
            # API returned a response but no transcript content — video has no captions.
            logger.info(f"{prefix}Supadata: No transcript available — marking permanent, will not retry")
            return False
    except Exception as exc:
        exc_str = str(exc).lower()
        # Detect quota / credit exhaustion from SupadataError or HTTP 402/429.
        is_quota_error = False
        if isinstance(exc, SupadataError):
            is_quota_error = any(
                kw in (exc.error or "").lower()
                for kw in ("credit", "quota", "payment", "limit", "billing")
            )
        elif isinstance(exc, requests.exceptions.HTTPError):
            status = getattr(getattr(exc, "response", None), "status_code", None)
            is_quota_error = status == 402

        # Detect permanent "no transcript" from SupadataError codes.
        is_permanent_no_transcript = isinstance(exc, SupadataError) and (
            exc.error == "transcript-unavailable"
            or exc.error == "forbidden"
            or "not-found" in (exc.error or "")
        )

        if is_quota_error:
            logger.error(
                f"{prefix}Supadata: Quota exhausted — no credits remaining. "
                f"Halting transcript fetches for this run."
            )
            if transcript_rate_limit_event is not None:
                transcript_rate_limit_event.set()
        elif is_permanent_no_transcript:
            logger.info(f"{prefix}Supadata: No transcript available — marking permanent, will not retry")
            return False
        elif "live streaming" in exc_str:
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
) -> list[GeminiStory] | bool | None:
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
        f"relevant to this FOCUS: <focus>{focus}</focus>.\n\n"
        "WRITING GUIDELINES:\n"
        "1. TONE: Professional, objective, and authoritative. Use the Inverted "
        "Pyramid style.\n"
        "2. CONTENT: Focus on the 'Why it Matters'. Skip ceremonial talk.\n"
        "3. DATELINE: Construct a formal AP-style dateline "
        "(e.g., 'SPRINGFIELD, Mo. — March 14, 2026').\n\n"
        "Return result ONLY as raw JSON list of objects with keys: "
        "'title', 'dateline', 'content', 'start_time_seconds', 'topics'. "
        "'topics' must be a list of 2-6 short lowercase keyword strings that "
        "categorise the story (e.g. [\"housing\", \"zoning\", \"budget\"])."
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
                logger.info(f"{prefix}Gemini: No stories returned (no JSON in response)")
                return []
            stories = json.loads(json_match.group(0))
            logger.info(f"{prefix}Gemini: {len(stories)} stor{'y' if len(stories) == 1 else 'ies'} generated")
            return stories
        elif response.status_code == 429:
            logger.warning(f"{prefix}Gemini: Rate limit hit — AI disabled for this run")
            return False
    except Exception as exc:
        logger.error(f"{prefix}Gemini: API call failed: {exc}")

    return None


def write_story_files(
    stories: list[GeminiStory],
    meeting_dir: Path,
    video_id: str = "",
    *,
    clear_existing: bool = True,
    start_index: int = 1,
) -> None:
    """Write each story dict as a numbered Markdown file inside *meeting_dir*.

    File names are ``01_<slug>.md``, ``02_<slug>.md``, …

    By default (*clear_existing=True*) any stale story files from a previous
    (failed) run are deleted first so results from different Gemini calls are
    not mixed.  Pass ``clear_existing=False, start_index=N`` when appending
    new stories from additional focus passes to an existing set.

    File format::

        # Story Title
        *AP-style dateline*
        **Source:** https://youtu.be/<video_id>?t=120

        Body text …

        ---
        **Segment Start:** 120s
    """
    if clear_existing:
        for old_file in meeting_dir.glob("[0-9]*.md"):
            old_file.unlink()

    for index, story in enumerate(stories, start=start_index):
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
            topics = story.get("topics") or []
            if topics:
                fh.write(f"**Topics:** {', '.join(str(t).lower().strip() for t in topics)}\n")
            story_user_ids = story.get("_user_ids") or []
            if story_user_ids:
                fh.write(f"**Users:** {', '.join(story_user_ids)}\n")


# ---------------------------------------------------------------------------
# RSS feed builders
# ---------------------------------------------------------------------------


def rebuild_feed(feed_dir: Path, feed_cfg: FeedConfig) -> None:
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
            try:
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
            except Exception as exc:
                logger.debug(f"Skipping {story_file}: {exc}")
                continue

    feed.rss_file(feed_dir / "rss.xml", pretty=True)
    (feed_dir / "channel.json").write_text(
        json.dumps({"channel_id": feed_cfg["channel_id"], "channel_name": feed_cfg["channel_name"]})
    )


def rebuild_aggregate_feed(base_url: str = "") -> None:
    """Aggregate stories from all channel folders into ``content/rss.xml``.

    Collects all stories across every channel sub-directory,
    ordered by processing timestamp (newest first).  Each entry is prefixed
    with ``[Channel Name]`` in the title so readers know the source.

    This function reads all channel directories and writes one shared file, so
    it must always run as a serial barrier after all channel threads have joined.
    ``_main_body`` calls it exactly once after ``ThreadPoolExecutor.map`` returns.

    Args:
        base_url: Public URL of this feed (used as the RSS ``<self>`` link).
                  Omit or pass an empty string to leave the self-link out.
    """
    logger.info("TubeNews: Rebuilding aggregate feed (content/rss.xml)")

    feed = FeedGenerator()
    feed.id("tubenews_meta_rss")
    feed.title("TubeNews: News Feed")
    feed.description("Aggregated news from monitored channels.")
    # feedgen requires at least one link; fall back to YouTube if no base_url.
    feed.link(href=base_url if base_url else "https://www.youtube.com", rel="alternate")
    if base_url:
        feed.link(href=base_url, rel="self")

    all_stories: list[dict] = []
    for channel_dir in [d for d in STORAGE_ROOT.iterdir() if d.is_dir() and not d.name.startswith("_")]:
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
            except Exception as exc:
                logger.debug(f"Skipping {meeting_dir}: {exc}")
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
        except Exception as exc:
            logger.debug(f"Skipping {entry['file']}: {exc}")
            continue

    feed.rss_file(STORAGE_ROOT / "rss.xml", pretty=True)


def build_user_feed_xml(user: dict, base_url: str = "", user_id: str = "", channel_focus: dict[str, str | list[str]] | None = None) -> bytes:
    """Build and return RSS feed XML bytes for a user's subscribed channels.

    Contains all the feed-building logic.  Does *not* write anything to disk —
    callers decide what to do with the returned bytes (serve directly or cache).

    Reads ``channel.json`` from each channel directory (written by :func:`rebuild_feed`)
    to determine which archive folders correspond to the user's subscribed channels.  Stories
    are sorted newest-first, matching :func:`rebuild_aggregate_feed` behaviour.

    Args:
        user:          User config dict from ``content/_users/<id>/user.json``.
                       Must contain ``name`` (str) and ``channels`` (dict[str, list[str]]).
        base_url:      Public URL root; currently unused but reserved for future self-links.
        user_id:       UUID directory name for the user. Falls back to slugify(name) if omitted.
        channel_focus: Optional override mapping ``{channel_id: list[str]}``.  When
                       ``None``, falls back to ``user.get("channels", {})``.
    """
    name = user["name"]
    channels_data = user.get("channels", {})
    subscribed = set(channels_data.keys())
    if channel_focus is None:
        channel_focus = channels_data

    feed = FeedGenerator()
    feed.id(f"tubenews_user_{slugify(name)}")
    feed.title(f"TubeNews: {name}")
    feed.description("Personalized TubeNews feed.")
    feed.link(href=base_url if base_url else "https://www.youtube.com", rel="alternate")

    all_stories: list[dict] = []
    for channel_dir in [d for d in STORAGE_ROOT.iterdir() if d.is_dir() and not d.name.startswith("_")]:
        channel_json = channel_dir / "channel.json"
        if not channel_json.exists():
            continue
        try:
            channel_info = json.loads(channel_json.read_text())
        except Exception as exc:
            logger.debug(f"Skipping {channel_json}: {exc}")
            continue
        channel_id = channel_info.get("channel_id")
        if channel_id not in subscribed:
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
                    all_stories.append({"file": story_file, "meta": metadata,
                                        "channel_name": channel_name, "channel_id": channel_id})
            except Exception as exc:
                logger.debug(f"Skipping {meeting_dir}: {exc}")
                continue

    all_stories.sort(key=lambda entry: entry["meta"].get("processed_at", 0), reverse=True)

    for entry in all_stories:
        try:
            story = parse_story_file(entry["file"])
            story_user_ids = story.get("user_ids", [])
            if story_user_ids and user_id not in story_user_ids:
                continue
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
        except Exception as exc:
            logger.debug(f"Skipping {entry['file']}: {exc}")
            continue

    return feed.rss_str(pretty=True)


def rebuild_user_feed(user: dict[str, object], base_url: str = "", user_id: str = "") -> None:
    """Write ``archive/users/<id>/rss.xml`` for a user's subscribed channels.

    Thin wrapper around :func:`build_user_feed_xml` that writes the result to
    disk.  Used by the CLI (``main()``) to pre-cache feeds after each run.
    The web app serves feeds dynamically via :func:`build_user_feed_xml` instead.

    Args:
        user:     User config dict from ``archive/users/<id>/user.json``.
        base_url: Public URL root; passed through to :func:`build_user_feed_xml`.
        user_id:  UUID directory name for the user. Falls back to slugify(name) if omitted.
    """
    name = user["name"]
    user_dir = STORAGE_ROOT / "_users" / (user_id or slugify(name))
    user_dir.mkdir(parents=True, exist_ok=True)
    logger.info(f"TubeNews: Rebuilding user feed for {name}")
    xml_bytes = build_user_feed_xml(user, base_url=base_url, user_id=user_id)
    (user_dir / "rss.xml").write_bytes(xml_bytes)


def rebuild_user_blog(user: dict[str, object], base_url: str = "", user_id: str = "") -> None:
    """Generate ``archive/users/<id>/index.html`` — a static blog page for a user.

    Pulls stories from the user's subscribed channels (same logic as
    :func:`rebuild_user_feed`) and renders them as a self-contained HTML page
    sorted newest-first.  All stories are included regardless of age.

    Args:
        user:      User config dict from ``archive/users/<id>/user.json``.
        base_url:  Public URL root; used to build a self-link in the page header.
        user_id:   UUID directory name for the user. Falls back to slugify(name) if omitted.
    """
    name = user["name"]
    subscribed = set(user.get("channels", {}).keys())
    user_dir = STORAGE_ROOT / "_users" / (user_id or slugify(name))
    user_dir.mkdir(parents=True, exist_ok=True)

    logger.info(f"TubeNews: Rebuilding blog for {name}")

    all_stories: list[dict] = []
    for channel_dir in [d for d in STORAGE_ROOT.iterdir() if d.is_dir() and not d.name.startswith("_")]:
        channel_json = channel_dir / "channel.json"
        if not channel_json.exists():
            continue
        try:
            channel_info = json.loads(channel_json.read_text())
        except Exception as exc:
            logger.debug(f"Skipping {channel_json}: {exc}")
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
                    all_stories.append({"file": story_file, "meta": metadata, "channel_name": channel_name,
                                        "channel_slug": channel_dir.name, "meeting_id": meeting_dir.name})
            except Exception as exc:
                logger.debug(f"Skipping {meeting_dir}: {exc}")
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
        except Exception as exc:
            logger.debug(f"Skipping {entry['file']}: {exc}")
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
        f"{len(all_stories)} stories from {len(subscribed)} channel{'s' if len(subscribed) != 1 else ''}"
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



MAX_FOCUSES_PER_CHANNEL = 10


def _needs_processing(video_id: str, feed_dir: Path) -> bool:
    """Return True if *video_id* needs processing (no ``metadata.json`` exists for it).

    A video needs processing when:

    - Its archive directory does not exist yet (new video).
    - The directory exists but has no ``metadata.json`` (transcript cached but
      the AI step failed on a previous run — recovery path).

    A video with any ``metadata.json`` (regardless of ``processed_focuses``) is
    considered done and will not be reprocessed.  New focus strings only apply
    to newly discovered videos going forward.
    """
    return not any(
        d.name.endswith(video_id)
        for d in feed_dir.iterdir()
        if d.is_dir() and (d / "metadata.json").exists()
    )


def _collect_channel_focuses(channel_id: str, feed_focus: str) -> list[tuple[str, list[str]]]:
    """Return ordered ``(focus, user_ids)`` pairs to use when processing *channel_id*.

    Each pair represents one Gemini call.  ``user_ids`` is the list of user
    UUID directory names whose focus produced this entry; an empty list means
    the focus came from the feed-level config and is unrestricted (all users
    see the resulting stories).

    Starts with *feed_focus* from ``TubeNews.json`` (unrestricted), then
    appends each unique user focus.  When multiple users share the same focus
    string their UUIDs are merged into a single entry so Gemini is only called
    once.  If a user focus matches the feed-level focus it is absorbed into the
    unrestricted entry.

    The list is capped at :data:`MAX_FOCUSES_PER_CHANNEL` and always contains
    at least one entry (``("", [])`` when nothing is configured).
    """
    # focus_string → user_ids ([] = unrestricted/feed-level)
    focus_map: dict[str, list[str]] = {}
    focus_order: list[str] = []

    if feed_focus and feed_focus.strip():
        f = feed_focus.strip()
        focus_map[f] = []  # feed-level: no user restriction
        focus_order.append(f)

    users_dir = STORAGE_ROOT / "_users"
    if users_dir.is_dir():
        for uid_dir in sorted(users_dir.iterdir()):
            if not uid_dir.is_dir():
                continue
            user_json = uid_dir / "user.json"
            if not user_json.exists():
                continue
            try:
                data = json.loads(user_json.read_text(encoding="utf-8"))
            except Exception as exc:
                logger.debug(f"Skipping {uid_dir.name}: {exc}")
                continue
            channels_data = data.get("channels", {})
            if channel_id not in channels_data:
                continue
            raw = channels_data.get(channel_id, [])
            for f in raw:
                f = f.strip()
                if not f:
                    continue
                if f in focus_map:
                    existing = focus_map[f]
                    if existing:  # user-restricted: add this user
                        focus_map[f] = existing + [uid_dir.name]
                    # if [] (feed-level/unrestricted), leave as-is
                else:
                    focus_map[f] = [uid_dir.name]
                    focus_order.append(f)

    focuses = [(f, focus_map[f]) for f in focus_order]

    if len(focuses) > MAX_FOCUSES_PER_CHANNEL:
        logger.warning(
            f"Channel {channel_id}: {len(focuses)} focuses exceed cap "
            f"({MAX_FOCUSES_PER_CHANNEL}); truncating"
        )
        focuses = focuses[:MAX_FOCUSES_PER_CHANNEL]

    return focuses or [("", [])]


def process_video(
    video_id: str,
    video_title: str,
    video_date: str,
    is_live: bool,
    feed: FeedConfig,
    feed_dir: Path,
    supadata_client: Supadata,
    config: dict,
    ai_disabled: bool,
    video_num: int = 0,
    total_videos: int = 0,
    focuses: list[tuple[str, list[str]]] | None = None,
    transcript_rate_limit_event: threading.Event | None = None,
) -> tuple[str, int]:
    """Fetch, analyse, and archive one video.

    Attempts to reuse a locally-cached transcript (from a previous run that
    completed the fetch but failed during AI analysis) before hitting the
    Supadata API again.

    *focuses* is a list of ``(focus_string, user_ids)`` pairs — one Gemini
    call is made per pair.  ``user_ids`` is the list of user UUID directory
    names whose focus produced this entry; ``[]`` means unrestricted
    (feed-level focus, shown to all users).  If omitted, falls back to a
    single unrestricted call using ``feed["focus"]``.

    Returns a ``(status, n_stories)`` tuple where *status* is one of:

        ``"content_written"``         – stories were generated and written to
                                        disk; *n_stories* is the count of new
                                        stories added.
        ``"ai_rate_limited"``         – Gemini returned 429; caller should set
                                        ``ai_disabled = True`` for the session;
                                        *n_stories* is 0.
        ``"transcript_quota_exhausted"`` – Supadata quota is exhausted;
                                        *transcript_rate_limit_event* has been
                                        set; caller should stop processing
                                        further videos; *n_stories* is 0.
        ``"skipped"``                 – transcript unavailable, live stream, AI
                                        disabled, or AI returned nothing;
                                        *n_stories* is 0.  When Gemini returns
                                        an empty story list, a
                                        ``metadata.json`` with
                                        ``status: "no_stories"`` is written so
                                        the video is not resubmitted to the AI
                                        on future runs.
    """
    if focuses is None:
        focuses = [(feed.get("focus", ""), [])]

    # Locate any pre-existing archive folder for this video ID.
    existing_dir = next(
        (d for d in feed_dir.iterdir() if d.is_dir() and d.name.endswith(video_id)),
        None,
    )

    channel_name = feed["channel_name"]

    # --- Load or fetch transcript ---
    if existing_dir and (existing_dir / "transcript.txt").exists():
        # Re-use cached transcript; only the AI step needs to re-run.
        logger.info(f"{channel_name}: [{video_id}] {video_title}: TubeNews: Found cached transcript, re-running AI")
        transcript_text = (existing_dir / "transcript.txt").read_text(encoding="utf-8")
        video_date = existing_dir.name.split("_")[0]
        meeting_dir = existing_dir
    else:
        # If quota was already known exhausted, don't attempt the API call.
        if transcript_rate_limit_event is not None and transcript_rate_limit_event.is_set():
            return "transcript_quota_exhausted", 0
        counter = f" ({video_num}/{total_videos})" if total_videos else ""
        logger.info(f"{channel_name}: [{video_id}] {video_title}: TubeNews: Processing new video{counter}")
        if is_live:
            logger.info(f"{channel_name}: [{video_id}] {video_title}: TubeNews: Live stream — skipping, will retry next run")
            return "skipped", 0
        logger.info(f"{channel_name}: [{video_id}] {video_title}: Supadata: Fetching transcript")
        transcript_text = fetch_transcript(
            video_id, supadata_client,
            feed_name=channel_name, video_title=video_title,
            transcript_rate_limit_event=transcript_rate_limit_event,
        )
        if transcript_text is False:
            # Permanent: Supadata confirmed no transcript exists — never retry.
            meeting_dir = feed_dir / f"{video_date}_{video_id}"
            meeting_dir.mkdir(exist_ok=True)
            metadata: MetadataDict = {
                "video_id": video_id,
                "video_title": video_title,
                "status": "no_transcript_available",
                "processed_at": time.time(),
            }
            (meeting_dir / "metadata.json").write_text(json.dumps(metadata))
            logger.info(f"{channel_name}: [{video_id}] {video_title}: TubeNews: No transcript available — marked permanent, will not retry")
            return "skipped", 0
        elif not transcript_text:
            # Transient failure — quota exhausted or network error, will retry next run.
            if transcript_rate_limit_event is not None and transcript_rate_limit_event.is_set():
                return "transcript_quota_exhausted", 0
            logger.info(f"{channel_name}: [{video_id}] {video_title}: Supadata: Fetch failed — will retry next run")
            return "skipped", 0

        meeting_dir = feed_dir / f"{video_date}_{video_id}"
        meeting_dir.mkdir(exist_ok=True)
        (meeting_dir / "transcript.txt").write_text(transcript_text, encoding="utf-8")

    # --- Generate news stories via Gemini ---
    if ai_disabled:
        return "skipped", 0

    # Call Gemini once per focus, deduplicating stories by title across passes.
    # Track title → index so we can merge user_ids when the same story appears
    # in multiple focus passes.
    seen_titles: dict[str, int] = {}
    all_stories: list = []
    gemini_delay = config.get("gemini_call_delay", 5)
    first_call = True
    for focus, user_ids in focuses:
        if not first_call and gemini_delay:
            time.sleep(gemini_delay)
        first_call = False
        label = f" (focus: {focus!r})" if len(focuses) > 1 else ""
        logger.info(f"{channel_name}: {video_title}: Gemini: Generating stories{label}")
        result = call_gemini_api(
            transcript_text=transcript_text,
            focus=focus,
            video_title=video_title,
            video_date=video_date,
            gemini_api_key=config["gemini_api_key"],
            model_name=config["gemini_model"],
            feed_name=channel_name,
        )
        if result is False:
            return "ai_rate_limited", 0
        for story in (result or []):
            title = story.get("title", "")
            if title in seen_titles:
                # Merge user_ids: unrestricted (feed-level) always wins
                existing = all_stories[seen_titles[title]]
                existing_uids: list[str] = existing.get("_user_ids", [])
                if not existing_uids or not user_ids:
                    existing["_user_ids"] = []  # unrestricted wins
                else:
                    existing["_user_ids"] = sorted(set(existing_uids) | set(user_ids))
            else:
                story["_user_ids"] = list(user_ids)
                seen_titles[title] = len(all_stories)
                all_stories.append(story)

    if all_stories:
        write_story_files(all_stories, meeting_dir, video_id)
        metadata = {
            "video_id": video_id,
            "video_title": video_title,
            "status": "processed",
            "processed_at": time.time(),
            "processed_focuses": sorted(f for f, _ in focuses),
        }
        (meeting_dir / "metadata.json").write_text(json.dumps(metadata))
        n = len(all_stories)
        logger.info(f"{channel_name}: [{video_id}] {video_title}: Done — {n} stor{'y' if n == 1 else 'ies'} written")
        return "content_written", n

    # Gemini returned no stories for any focus.
    metadata = {
        "video_id": video_id,
        "video_title": video_title,
        "status": "no_stories",
        "processed_at": time.time(),
        "processed_focuses": sorted(f for f, _ in focuses),
    }
    (meeting_dir / "metadata.json").write_text(json.dumps(metadata))
    logger.info(f"{channel_name}: [{video_id}] {video_title}: Done — no relevant stories found")
    return "skipped", 0


def process_feed(
    feed: FeedConfig,
    supadata_client: Supadata,
    config: dict,
    ai_rate_limit_event: threading.Event | None = None,
    transcript_rate_limit_event: threading.Event | None = None,
) -> tuple[bool, bool, int]:
    """Process all new videos for one configured channel.

    Discovers videos from YouTube, skips ones already archived, and calls
    :func:`process_video` for each new one.

    *ai_rate_limit_event* is an optional shared :class:`threading.Event`.
    When set (by any channel hitting a 429), all channels skip further AI
    calls for the remainder of the run.  Pass ``None`` for single-channel use.

    *transcript_rate_limit_event* is an optional shared :class:`threading.Event`.
    When set (Supadata quota exhausted), processing stops immediately for all
    remaining videos across all channels.

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

    # Collect the union of all focus strings for this channel (feed config +
    # all subscriber preferences), capped at MAX_FOCUSES_PER_CHANNEL.
    focuses = _collect_channel_focuses(feed["channel_id"], feed.get("focus", ""))

    all_videos = discover_videos(feed["channel_id"], feed_name=channel_name)
    if not all_videos:
        return content_changed, ai_rate_limited, stories_written

    all_ids = [v["id"] for v in all_videos]
    video_meta = {v["id"]: v for v in all_videos}

    # Videos without metadata.json — new or in recovery (transcript cached, AI failed).
    unprocessed = [v for v in all_videos if _needs_processing(v["id"], feed_dir)]

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
        for v in fresh:
            logger.info(f"  held: {v['id']} (parsed date: {v['date']}) — {v['title']}")

    if is_new_feed:
        too_old_count = len([v for v in unprocessed if all_ids.index(v["id"]) > 0])
        if too_old_count:
            logger.info(f"{channel_name}: TubeNews: New feed — marking {too_old_count} existing video(s) as too old to process")

    # Videos that will actually be processed (not too old, not too fresh).
    videos_to_process = [
        v for v in unprocessed
        if not (is_new_feed and all_ids.index(v["id"]) > 0)
        and v["date"] != today_str
    ]
    total = len(videos_to_process)

    for video_info in unprocessed:
        # On a brand-new feed, mark all but the most recent video as
        # ignored_too_old so the first run doesn't process months of
        # old meetings.
        if is_new_feed and all_ids.index(video_info["id"]) > 0:
            stub_dir = feed_dir / f"2000-01-01_{video_info['id']}"
            stub_dir.mkdir(exist_ok=True)
            (stub_dir / "metadata.json").write_text(json.dumps({
                "video_id": video_info["id"],
                "status": "ignored_too_old",
                "processed_at": time.time(),
            }))
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
            focuses=focuses,
            transcript_rate_limit_event=transcript_rate_limit_event,
        )

        if result == "content_written":
            content_changed = True
            stories_written += n
        elif result == "ai_rate_limited":
            ai_rate_limited = True
            if ai_rate_limit_event is not None:
                ai_rate_limit_event.set()
        elif result == "transcript_quota_exhausted":
            # Event already set by process_video; stop wasting time on this feed.
            break

    return content_changed, ai_rate_limited, stories_written


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def _send_ntfy(topic: str, total_stories: int, feed_results: list[FeedResult], started_at: float) -> None:
    """POST a run-summary notification to ntfy.sh/<topic>."""
    import urllib.request as _urllib_request

    timestamp = _fmt_no_leading_zeros(datetime.fromtimestamp(started_at), "%B %d, %Y at %I:%M %p")
    story_word = "story" if total_stories == 1 else "stories"
    lines = [f"{total_stories} new {story_word} — {timestamp}"]
    for r in feed_results:
        if r["stories_written"]:
            lines.append(f"  \u2022 {r['channel_name']}: {r['stories_written']}")
    message = "\n".join(lines)

    req = _urllib_request.Request(
        f"https://ntfy.sh/{topic}",
        data=message.encode(),
        method="POST",
        headers={"Title": "TubeNews", "Priority": "default"},
    )
    try:
        _urllib_request.urlopen(req, timeout=10)
        logger.debug(f"ntfy.sh/{topic}: notification sent")
    except Exception as exc:
        logger.warning(f"ntfy.sh/{topic}: notification failed: {exc}")


# ---------------------------------------------------------------------------
# Process locking — prevent concurrent runs
# ---------------------------------------------------------------------------

LOCK_FILE = STORAGE_ROOT / ".tubenews.lock"


def _acquire_lock() -> bool:
    """Atomically create the lock file containing this process's PID.

    Returns True if the lock was acquired.  Returns False if another live
    process already holds it.  Stale locks (dead PID) are removed and the
    acquire is retried once.
    """
    try:
        fd = os.open(str(LOCK_FILE), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
        return True
    except FileExistsError:
        try:
            pid = int(LOCK_FILE.read_text().strip())
            os.kill(pid, 0)          # raises if process is gone
            return False             # process is alive — lock is valid
        except (ValueError, ProcessLookupError, PermissionError):
            LOCK_FILE.unlink(missing_ok=True)   # stale lock
            return _acquire_lock()


def _release_lock() -> None:
    LOCK_FILE.unlink(missing_ok=True)


def main() -> None:
    """Load config, process each feed, and rebuild RSS outputs."""
    parser = argparse.ArgumentParser(description="TubeNews — YouTube channel monitor")
    parser.add_argument("--debug", action="store_true", help="Enable verbose debug output")
    args = parser.parse_args()

    setup_logging(args.debug)

    if not _acquire_lock():
        logger.error("TubeNews: Another instance is already running. Exiting.")
        return

    try:
        _main_body(args)
    finally:
        _release_lock()


def _main_body(args) -> None:
    """Core run logic, called from main() after the lock is acquired."""
    with open(CONFIG_FILE, "r") as config_file:
        config = json.load(config_file)

    # Reject duplicate channel_ids before spawning threads — two entries for
    # the same channel would race to process the same video directories.
    seen_ids: dict[str, str] = {}
    for feed in config.get("feeds", []):
        cid = feed.get("channel_id", "")
        cname = feed.get("channel_name", "?")
        if cid in seen_ids:
            logger.error(
                f"TubeNews: Duplicate channel_id '{cid}' in feeds "
                f"('{seen_ids[cid]}' and '{cname}'). "
                "Fix TubeNews.json and re-run."
            )
            return
        seen_ids[cid] = cname

    supadata_client = Supadata(api_key=config["supadata_api_key"])
    logger.info(f"Session Start | {_fmt_no_leading_zeros(datetime.now(), '%A, %B %d, %Y')} | AI Model: {config.get('gemini_model')}")

    # Check cached Supadata balance before doing any work.
    quota_ok, cached_balance = _check_supadata_quota(config)
    started_at = time.time()
    if not quota_ok:
        run_log_path = STORAGE_ROOT / "_run_logs" / "run_log.json"
        run_log_path.parent.mkdir(exist_ok=True)
        try:
            runs = json.loads(run_log_path.read_text()) if run_log_path.exists() else []
        except Exception:
            runs = []
        runs.append({
            "started_at": started_at,
            "finished_at": time.time(),
            "total_stories": 0,
            "ai_rate_limited": False,
            "transcript_quota_exhausted": True,
            "feeds": [],
            "pid": os.getpid(),
        })
        try:
            run_log_path.write_text(json.dumps(runs[-30:], indent=2))
        except Exception as exc:
            logger.warning(f"TubeNews: Failed to write run log: {exc}")
        return

    ai_rate_limit_event = threading.Event()
    transcript_rate_limit_event = threading.Event()
    any_content_changed = threading.Event()

    def _run_feed(feed: FeedConfig) -> FeedResult:
        try:
            content_changed, _, stories_written = process_feed(
                feed, supadata_client, config, ai_rate_limit_event,
                transcript_rate_limit_event,
            )
            if content_changed:
                rebuild_feed(STORAGE_ROOT / slugify(feed["channel_name"]), feed)
                any_content_changed.set()
            return {
                "channel_id": feed["channel_id"],
                "channel_name": feed["channel_name"],
                "stories_written": stories_written,
            }
        except Exception:
            logger.warning(
                f"{feed.get('channel_name', feed.get('channel_id', '?'))}: "
                "TubeNews: Feed processing failed — skipping"
            )
            return {
                "channel_id": feed.get("channel_id", ""),
                "channel_name": feed.get("channel_name", ""),
                "stories_written": 0,
            }

    max_workers = min(len(config["feeds"]), config.get("max_parallel_feeds", 1))
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        feed_results = list(executor.map(_run_feed, config["feeds"]))
    total_stories = sum(r["stories_written"] for r in feed_results)

    if any_content_changed.is_set() or not (STORAGE_ROOT / "rss.xml").exists():
        try:
            rebuild_aggregate_feed(base_url=config.get("base_url", ""))
        except Exception:
            logger.warning("TubeNews: Meta feed rebuild failed — skipping; user feeds will still be rebuilt")

    users_dir = STORAGE_ROOT / "_users"
    if users_dir.is_dir():
        for user_json in sorted(users_dir.glob("*/user.json")):
            try:
                user = json.loads(user_json.read_text())
                uid = user_json.parent.name
                rebuild_user_feed(user, base_url=config.get("base_url", ""), user_id=uid)
            except Exception:
                logger.warning(f"TubeNews: Failed to rebuild feed for user {user_json.parent.name} — skipping")

    story_word = "story" if total_stories == 1 else "stories"
    logger.info(f"Session End. {total_stories} new {story_word} published.")

    run_log_path = STORAGE_ROOT / "_run_logs" / "run_log.json"
    run_log_path.parent.mkdir(exist_ok=True)
    try:
        runs = json.loads(run_log_path.read_text()) if run_log_path.exists() else []
    except Exception as exc:
        logger.warning(f"Failed to load run log; starting fresh: {exc}")
        runs = []
    runs.append({
        "started_at": started_at,
        "finished_at": time.time(),
        "total_stories": total_stories,
        "ai_rate_limited": ai_rate_limit_event.is_set(),
        "transcript_quota_exhausted": transcript_rate_limit_event.is_set(),
        "feeds": feed_results,
        "pid": os.getpid(),
    })
    retained = runs[-30:]
    try:
        run_log_path.write_text(json.dumps(retained, indent=2))
    except Exception as exc:
        logger.warning(f"TubeNews: Failed to write run log: {exc}")

    # Prune run-<pid>.log files that are no longer referenced by the retained runs.
    kept_pids = {str(r["pid"]) for r in retained if "pid" in r}
    for log_file in run_log_path.parent.glob("run-*.log"):
        pid_str = log_file.stem[4:]  # strip leading "run-"
        if pid_str not in kept_pids:
            try:
                log_file.unlink()
            except Exception as exc:
                logger.debug(f"Could not remove old run log {log_file.name}: {exc}")

    ntfy_topic = config.get("ntfy_topic")
    if ntfy_topic and total_stories > 0:
        _send_ntfy(ntfy_topic, total_stories, feed_results, started_at)

    _cache_supadata_balance(config)


def _check_supadata_quota(config: dict) -> tuple[bool, dict | None]:
    """Check Supadata credit balance before starting a run.

    Reads the balance cached at the end of the previous run
    (``content/_run_logs/supadata_balance.json``) — no live API call is made here so no
    credits are consumed.  If the file is absent (first run) we proceed
    optimistically; the end-of-run cache will populate it for next time.

    Returns:
        ``(ok, balance)`` — *ok* is True when there are credits remaining
        (or when the balance cannot be determined), *balance* is the raw
        cached dict or None.  When *ok* is False the caller should abort
        and record ``transcript_quota_exhausted`` in the run log.
    """
    balance_path = STORAGE_ROOT / "_run_logs" / "supadata_balance.json"
    if not balance_path.exists():
        return True, None
    try:
        balance = json.loads(balance_path.read_text())
    except Exception:
        return True, None
    max_credits = balance.get("maxCredits", 0)
    used_credits = balance.get("usedCredits", 0)
    remaining = max_credits - used_credits
    if max_credits > 0 and remaining <= 0:
        reset_date = balance.get("resetDate", "unknown")
        logger.error(
            f"TubeNews: Supadata quota exhausted (0 of {max_credits} credits remaining). "
            f"Resets: {reset_date}. Aborting run — no transcripts can be fetched."
        )
        return False, balance
    if max_credits > 0:
        pct_used = used_credits / max_credits * 100
        if pct_used >= 90:
            logger.warning(
                f"TubeNews: Supadata credits low — {remaining} of {max_credits} remaining "
                f"({pct_used:.0f}% used)."
            )
    return True, balance


def _cache_supadata_balance(config: dict) -> None:
    """Fetch Supadata credit usage and cache it to ``content/_run_logs/supadata_balance.json``.

    Called once at the end of each ``main()`` run so the web UI can read the
    cached result instantly instead of making a live API call on every page load.
    Silently skips if the API key is absent or the request fails.
    """
    key = config.get("supadata_api_key", "")
    if not key:
        return
    try:
        resp = requests.get(
            "https://api.supadata.ai/v1/me",
            headers={"x-api-key": key},
            timeout=10,
        )
        if resp.status_code == 200:
            (STORAGE_ROOT / "_run_logs" / "supadata_balance.json").write_text(
                json.dumps(resp.json())
            )
            logger.debug("Supadata balance cached successfully.")
        else:
            logger.warning(
                f"Supadata balance: HTTP {resp.status_code} from /v1/me — "
                f"response body: {resp.text[:200]}"
            )
    except Exception as exc:
        logger.warning(f"Supadata balance: request failed — {exc}")


if __name__ == "__main__":
    main()
