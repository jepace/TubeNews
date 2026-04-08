#!/usr/bin/env python3
"""TubeNews — monitor YouTube channels for new videos and generate news feeds.

Workflow for each configured channel (feed):
  1. Discover recent video IDs via YouTube's official Atom RSS feed.
  2. Skip videos already in the local archive.
  3. For genuinely new videos: fetch a transcript via the Supadata API.
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
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import TypedDict

# ---------------------------------------------------------------------------
# Third-party imports
# ---------------------------------------------------------------------------
import pytz
import requests
from feedgen.feed import FeedGenerator
from supadata import Supadata, SupadataError

# ---------------------------------------------------------------------------
# Environment & paths
# ---------------------------------------------------------------------------

BASE_DIR = Path(__file__).resolve().parent
CONFIG_FILE = BASE_DIR / "TubeNews.json"

from tubenews_utils import resolve_roots  # noqa: E402


def _resolve_early_config(config_file: Path, base_dir: Path) -> tuple[Path, Path, int]:
    """Read path/network settings from *config_file* before main() runs.

    Returns ``(STORAGE_ROOT, STATE_ROOT, REQUEST_TIMEOUT)``.  All keys are
    optional; sensible defaults are returned when the file is absent or a key
    is missing.

    Args:
        config_file: Path to TubeNews.json.
        base_dir:    Directory used to resolve relative path keys.
    """
    storage_root, state_root = resolve_roots(config_file, base_dir)
    try:
        cfg = json.loads(config_file.read_text())
        request_timeout = int(cfg.get("request_timeout", 15))
    except Exception as exc:
        logging.warning(f"Failed to load config; using defaults: {exc}")
        request_timeout = 15
    return storage_root, state_root, request_timeout


STORAGE_ROOT, STATE_ROOT, REQUEST_TIMEOUT = _resolve_early_config(CONFIG_FILE, BASE_DIR)
STATE_ROOT.mkdir(parents=True, exist_ok=True)

# FreeBSD ships its CA bundle in a non-standard location; tell Python where
# to find it so HTTPS requests succeed. On Linux/macOS this path won't exist
# and the assignment is skipped.
_FREEBSD_CERT = "/usr/local/share/certs/ca-root-nss.crt"
if os.path.exists(_FREEBSD_CERT):
    os.environ["SSL_CERT_FILE"] = _FREEBSD_CERT

# Apply the timeout as the process-wide socket default so every network call
# (including Supadata's underlying HTTP) respects it automatically.
socket.setdefaulttimeout(REQUEST_TIMEOUT)

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


def _add_run_log_file_handler() -> None:
    """Add a FileHandler writing to state/run_logs/run-<pid>.log.

    Called after the lock is acquired so only actual runs produce a log file.
    The file is the same one the web UI's admin_run_log view reads, so both
    web-UI-triggered runs and cron runs appear identically in the Runs page.
    """
    log_dir = STATE_ROOT / "run_logs"
    log_dir.mkdir(exist_ok=True)
    log_path = log_dir / f"run-{os.getpid()}.log"
    fh = logging.FileHandler(log_path, encoding="utf-8")
    fh.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)s: %(message)s", datefmt="%H:%M:%S"
    ))
    logging.getLogger().addHandler(fh)


def _setup_daemon_logging() -> None:
    """Set up rotating file logging for daemon mode.

    Writes to state/run_logs/tubenews_daemon.log with rotation at 10MB.
    Keeps up to 5 backup files. Daemon mode runs indefinitely, so we use
    RotatingFileHandler instead of a fixed per-run file.
    """
    from logging.handlers import RotatingFileHandler

    log_dir = STATE_ROOT / "run_logs"
    log_dir.mkdir(exist_ok=True)
    log_path = log_dir / "tubenews_daemon.log"

    # Rotate at 10MB, keep 5 backup files
    handler = RotatingFileHandler(
        log_path,
        maxBytes=10 * 1024 * 1024,  # 10 MB
        backupCount=5,
        encoding="utf-8"
    )
    handler.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)s: %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
    ))
    logging.getLogger().addHandler(handler)


# ---------------------------------------------------------------------------
# Data contracts (TypedDicts)
# ---------------------------------------------------------------------------


class VideoInfo(TypedDict):
    """One discovered video entry from :func:`discover_videos`."""
    id: str
    title: str
    date: str


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
    published: str


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


def now_utc_iso() -> str:
    """Return current UTC time as ISO 8601 string with Z suffix.

    Format: YYYY-MM-DDTHH:MM:SSZ (e.g., 2026-04-07T00:14:36Z)
    """
    return datetime.now(timezone.utc).isoformat(timespec='seconds').replace('+00:00', 'Z')


def unix_to_iso8601(unix_ts: float | int) -> str:
    """Convert Unix timestamp to ISO 8601 UTC string with Z suffix.

    Args:
        unix_ts: Seconds since epoch (as float or int)

    Returns:
        ISO 8601 string: YYYY-MM-DDTHH:MM:SSZ
    """
    return datetime.fromtimestamp(unix_ts, tz=timezone.utc).isoformat(timespec='seconds').replace('+00:00', 'Z')


def iso8601_to_unix(iso_str: str | None) -> float | None:
    """Convert ISO 8601 UTC string to Unix timestamp.

    Args:
        iso_str: ISO 8601 string (with Z or +00:00 suffix), or None

    Returns:
        Unix timestamp as float, or None if input is None/empty
    """
    if not iso_str:
        return None
    return datetime.fromisoformat(iso_str.replace('Z', '+00:00')).timestamp()


def _get_timezone() -> str:
    """Get configured timezone for display (IANA name, e.g., 'America/Los_Angeles').

    Returns:
        Timezone string from config, or 'UTC' if not set or invalid.
    """
    try:
        return json.loads(CONFIG_FILE.read_text()).get("timezone", "UTC")
    except Exception:
        return "UTC"


def is_ripe(queued_at_iso: str | None, min_age_minutes: int) -> bool:
    """Check if a queued entry is old enough to process.

    Args:
        queued_at_iso: ISO 8601 timestamp when entry was queued, or None for immediate processing
        min_age_minutes: Minimum age in minutes before processing

    Returns:
        True if entry is old enough or immediately processable (None timestamp)
    """
    if queued_at_iso is None:
        return True  # null timestamp = process immediately
    try:
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=min_age_minutes)
        # Handle both ISO 8601 strings and legacy Unix floats
        if isinstance(queued_at_iso, (int, float)):
            # Legacy Unix timestamp
            return datetime.fromtimestamp(queued_at_iso, tz=timezone.utc) <= cutoff
        else:
            # ISO 8601 string
            return datetime.fromisoformat(queued_at_iso.replace('Z', '+00:00')) <= cutoff
    except (ValueError, TypeError, AttributeError):
        return True  # Invalid timestamp = process immediately


def _get_timestamp_as_float(ts_value: str | float | int | None) -> float:
    """Convert any timestamp format to Unix float for display/calculations.

    Handles both legacy Unix timestamps (float/int) and new ISO 8601 strings.
    Used for backward compatibility when reading stored timestamps.

    Args:
        ts_value: ISO 8601 string, Unix float/int, or None

    Returns:
        Unix timestamp as float; current time if input is None/invalid
    """
    if ts_value is None:
        return time.time()
    if isinstance(ts_value, (int, float)):
        return float(ts_value)  # Already Unix
    if isinstance(ts_value, str):
        result = iso8601_to_unix(ts_value)
        return result if result is not None else time.time()
    return time.time()  # Fallback for unknown types


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
        if l.strip() != "---"
        and not l.startswith("**Segment Start:**")
        and not l.startswith("**Source:**")
        and not l.startswith("**Topics:**")
        and not l.startswith("**Users:**")
        and not l.startswith("Published")
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

    published_match = re.search(r"^Published\s+(.+)$", text, re.MULTILINE)
    published = published_match.group(1).strip() if published_match else ""

    return {
        "title": title,
        "dateline": dateline,
        "body_html": body_html,
        "start_seconds": start_seconds,
        "topics": topics,
        "user_ids": user_ids,
        "published": published,
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

_YT_RSS_NS = {
    "atom": "http://www.w3.org/2005/Atom",
    "yt":   "http://www.youtube.com/xml/schemas/2015",
}

# Browser-like headers for YouTube requests.  YouTube doesn't actively block
# programmatic access to public RSS feeds or redirect checks, but sending a
# realistic User-Agent prevents the most basic bot-detection heuristics.
_YT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

# Global rate limiter for Gemini API calls.  Shared across all videos and
# focus passes so the minimum gap is enforced even between different videos
# processed in the same daemon cycle.
_gemini_rate_lock = threading.Lock()
_gemini_last_call_time: float = 0.0


def _is_youtube_short(video_id: str, feed_name: str = "") -> bool:
    """Return True if *video_id* is a YouTube Short.

    Fetches ``https://www.youtube.com/shorts/<id>`` with redirects enabled.
    YouTube keeps Shorts at that URL; regular videos redirect to ``/watch``.

    Fails open: returns ``False`` on any network or HTTP error so a transient
    failure never causes a real meeting video to be permanently skipped.
    """
    shorts_url = f"https://www.youtube.com/shorts/{video_id}"
    prefix = f"{feed_name}: " if feed_name else ""
    try:
        with requests.get(shorts_url, allow_redirects=True, stream=True,
                          timeout=REQUEST_TIMEOUT, headers=_YT_HEADERS) as resp:
            return "/shorts/" in resp.url
    except Exception as exc:
        logger.debug(f"{prefix}[{video_id}] Short check failed (treating as non-Short): {exc}")
        return False


def discover_videos(channel_id: str, feed_name: str = "") -> list[VideoInfo]:
    """Fetch channel videos from YouTube's official Atom RSS feed.

    URL: ``https://www.youtube.com/feeds/videos.xml?channel_id=CHANNEL_ID``

    Returns up to 15 most-recent entries (the YouTube RSS limit), newest-first.
    No API key, no HTML parsing, and no bot-detection headers required.

    Live streams that are still in progress will not have a transcript ready
    yet; :func:`fetch_transcript` handles that case by returning ``None``
    (transient failure) so the video is retried on the next run.

    Returns an ordered list of dicts::

        {"id": str, "title": str, "date": "YYYY-MM-DD"}
    """
    url = f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"
    prefix = f"{feed_name}: " if feed_name else ""
    resp = None
    for attempt in range(3):
        logger.debug(f"{prefix}YouTube RSS: Fetching feed" + (f" (retry {attempt})" if attempt else ""))
        try:
            r = requests.get(url, timeout=REQUEST_TIMEOUT, headers=_YT_HEADERS)
            if r.status_code == 200:
                resp = r
                break
            logger.warning(f"{prefix}YouTube RSS: HTTP {r.status_code}")
        except Exception as exc:
            if attempt < 2:
                time.sleep(2 ** attempt)
            else:
                logger.warning(f"{prefix}YouTube RSS: Failed after 3 attempts: {exc}")
    if resp is None:
        return []
    try:
        root = ET.fromstring(resp.text)
    except ET.ParseError as exc:
        logger.warning(f"{prefix}YouTube RSS: Could not parse feed XML: {exc}")
        return []
    videos: list[VideoInfo] = []
    for entry in root.findall("atom:entry", _YT_RSS_NS):
        vid_el   = entry.find("yt:videoId",    _YT_RSS_NS)
        title_el = entry.find("atom:title",    _YT_RSS_NS)
        pub_el   = entry.find("atom:published", _YT_RSS_NS)
        if vid_el is None or not vid_el.text:
            continue
        pub = (pub_el.text or "").strip() if pub_el is not None else ""
        date = pub if pub else datetime.now().strftime("%Y-%m-%d")
        videos.append({
            "id":    vid_el.text.strip(),
            "title": (title_el.text or "").strip() if title_el is not None else "",
            "date":  date,
        })
    if not videos:
        logger.warning(f"{prefix}YouTube RSS: Feed returned 0 entries — channel ID may be wrong")
    return videos



def fetch_transcript(
    video_id: str,
    supadata_client: Supadata,
    feed_name: str = "",
    video_title: str = "",
    transcript_rate_limit_event: threading.Event | None = None,
    failure_reason: list[str] | None = None,
    livestream_error: list[bool] | None = None,
) -> str | None | bool:
    """Fetch timed transcript segments from the Supadata API.

    Each segment is formatted as ``"<offset_seconds>s --> <text>"`` so Gemini
    knows where each sentence occurs in the video timeline.

    When a quota-exhausted error is detected (HTTP 402 or a
    ``SupadataError`` whose ``error`` code suggests credit exhaustion),
    *transcript_rate_limit_event* is set so that ``process_video`` and
    ``process_feed`` can abort remaining videos immediately.

    When a livestream error is detected (video is currently broadcasting),
    *livestream_error* is set to [True] to signal that the video should be
    deferred without incrementing retry_count.

    Returns:
        str  — formatted transcript on success.
        None — transient failure (network error, rate limit, livestream, etc.); will retry next run.
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
            if failure_reason is not None:
                failure_reason.append("no_captions")
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
                f"Halting transcript fetches for this cycle."
            )
            if transcript_rate_limit_event is not None:
                transcript_rate_limit_event.set()
        elif is_permanent_no_transcript:
            error_code = getattr(exc, "error", "") or ""
            if "forbidden" in error_code:
                reason = "members_only_or_restricted"
            elif "not-found" in error_code:
                reason = "video_not_found"
            else:
                reason = "no_captions"
            logger.info(f"{prefix}Supadata: No transcript available ({reason}) — marking permanent, will not retry")
            if failure_reason is not None:
                failure_reason.append(reason)
            return False
        elif "live streaming" in exc_str:
            logger.warning(f"{prefix}Supadata: Live stream — transcript unavailable, will retry later")
            if livestream_error is not None:
                livestream_error.append(True)
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
    call_delay: float = 8.0,
) -> list[GeminiStory] | bool | None:
    """Send a transcript to Google Gemini and parse the returned news stories.

    The prompt instructs Gemini to act as an investigative reporter and return
    a JSON list of story objects.  We ask for raw JSON (no markdown fences) so
    it can be parsed directly.

    Returns:
        list  – one dict per story on success.
        False – caller should disable AI for the remainder of this processing
                cycle because the API returned HTTP 429 (rate-limited).
        None  – any other failure; the caller should skip this video.
    """
    global _gemini_last_call_time
    with _gemini_rate_lock:
        elapsed = time.time() - _gemini_last_call_time
        if elapsed < call_delay:
            time.sleep(call_delay - elapsed)
        _gemini_last_call_time = time.time()

    api_version = "v1beta" if "preview" in model_name else "v1"
    api_url = (
        f"https://generativelanguage.googleapis.com/{api_version}/models/"
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
            logger.warning(f"{prefix}Gemini: Rate limit hit (429) — AI disabled for this cycle")
            return False
    except Exception as exc:
        logger.error(f"{prefix}Gemini: API call failed: {exc}")

    return None


def write_story_files(
    stories: list[GeminiStory],
    meeting_dir: Path,
    video_id: str = "",
    *,
    video_date: str = "",
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
        Published April 5, 2026 at 3:15 PM EST
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
            # Record when TubeNews wrote this story (not the YouTube publish date).
            # Convert current UTC time to configured timezone for display.
            pub_now_utc = datetime.now(timezone.utc)
            tz_name = _get_timezone()
            try:
                tz = pytz.timezone(tz_name)
                pub_now = pub_now_utc.astimezone(tz)
            except Exception:
                pub_now = pub_now_utc.astimezone(pytz.timezone("UTC"))
            tz_abbr = pub_now.strftime("%Z")
            pub_formatted = (
                _fmt_no_leading_zeros(pub_now, "%B %d, %Y")
                + " at "
                + _fmt_no_leading_zeros(pub_now, "%I:%M %p")
                + f" {tz_abbr} ({tz_name})"
            )
            fh.write(f"Published {pub_formatted}\n")
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
                    _get_timestamp_as_float(entry["meta"].get("processed_at"))
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
                    _get_timestamp_as_float(entry["meta"].get("processed_at"))
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
    user_dir = STATE_ROOT / "users" / (user_id or slugify(name))
    user_dir.mkdir(parents=True, exist_ok=True)
    logger.info(f"TubeNews: Rebuilding user feed for {name}")
    xml_bytes = build_user_feed_xml(user, base_url=base_url, user_id=user_id)
    (user_dir / "rss.xml").write_bytes(xml_bytes)


def rebuild_user_feed_page(user: dict[str, object], base_url: str = "", user_id: str = "") -> None:
    """Generate ``archive/users/<id>/index.html`` — a static feed page for a user.

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
    user_dir = STATE_ROOT / "users" / (user_id or slugify(name))
    user_dir.mkdir(parents=True, exist_ok=True)

    logger.info(f"TubeNews: Rebuilding feed page for {name}")

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
        .feed-content { max-width: 740px; margin: 0 auto; padding: 30px 20px 40px; }
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

    page_title = user.get("feed_name") or f"TubeNews — {name}"
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
        nav.feed-nav {{
            background: #fff; border-bottom: 1px solid #d1d5db;
            padding: 0 1.5rem; height: 52px;
            display: flex; align-items: center; justify-content: space-between;
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
        }}
        nav.feed-nav .nav-left {{ display: flex; align-items: center; gap: 1.5rem; }}
        nav.feed-nav a {{ color: #2563eb; text-decoration: none; font-size: 0.9rem; }}
        nav.feed-nav a:hover {{ text-decoration: underline; }}
        nav.feed-nav .nav-brand {{ font-weight: 700; font-size: 1.1rem; }}
        nav.feed-nav .nav-rss {{ display: flex; align-items: center; }}
        {CSS}
</style>
</head>
<body>
<nav class="feed-nav">
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
<div class="feed-content">
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

    users_dir = STATE_ROOT / "users"
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
    feed: FeedConfig,
    feed_dir: Path,
    supadata_client: Supadata,
    config: dict,
    ai_disabled: bool,
    video_num: int = 0,
    total_videos: int = 0,
    focuses: list[tuple[str, list[str]]] | None = None,
    transcript_rate_limit_event: threading.Event | None = None,
    channel_id: str = "",
    scheduled_start: str | None = None,
    raw_entry_xml: str = "",
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
        ``"skipped"``                 – transcript unavailable, AI disabled,
                                        or AI returned nothing;
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
        if _is_youtube_short(video_id, feed_name=channel_name):
            logger.info(f"{channel_name}: [{video_id}] {video_title}: TubeNews: YouTube Short — skipping permanently")
            short_dir = feed_dir / f"{video_date}_{video_id}"
            short_dir.mkdir(exist_ok=True)
            (short_dir / "metadata.json").write_text(json.dumps({
                "video_id": video_id,
                "video_title": video_title,
                "status": "ignored_short",
                "processed_at": now_utc_iso(),
            }))
            return "skipped", 0
        logger.info(f"{channel_name}: [{video_id}] {video_title}: Supadata: Fetching transcript")
        _transcript_failure_reason: list[str] = []
        _livestream_error: list[bool] = []
        transcript_text = fetch_transcript(
            video_id, supadata_client,
            feed_name=channel_name, video_title=video_title,
            transcript_rate_limit_event=transcript_rate_limit_event,
            failure_reason=_transcript_failure_reason,
            livestream_error=_livestream_error,
        )
        if transcript_text is False:
            # Supadata says no transcript exists. For recently published videos
            # (< 48 h) the captions may simply not be ready yet — live streams
            # end hours after the push notification fires and YouTube takes time
            # to process captions. Treat those as transient so the daemon retries.
            try:
                from datetime import datetime as _dt
                pub_dt = _dt.strptime(video_date, "%Y-%m-%d")
                age_hours = (_dt.now() - pub_dt).total_seconds() / 3600
            except Exception:
                age_hours = float("inf")
            if age_hours < 48:
                logger.info(
                    f"{channel_name}: [{video_id}] {video_title}: TubeNews: "
                    f"No transcript yet — video is only {age_hours:.0f}h old, will retry"
                )
                return "skipped", 0
            # Old enough — permanent.
            meeting_dir = feed_dir / f"{video_date}_{video_id}"
            meeting_dir.mkdir(exist_ok=True)
            skip_reason = _transcript_failure_reason[0] if _transcript_failure_reason else "no_captions"
            metadata: MetadataDict = {
                "video_id": video_id,
                "video_title": video_title,
                "status": "no_transcript_available",
                "skip_reason": skip_reason,
                "processed_at": now_utc_iso(),
            }
            (meeting_dir / "metadata.json").write_text(json.dumps(metadata))
            logger.info(f"{channel_name}: [{video_id}] {video_title}: TubeNews: No transcript available — marked permanent, will not retry")
            return "skipped", 0
        elif not transcript_text:
            # Transient failure — quota exhausted, livestream, or network error.
            if _livestream_error and _livestream_error[0]:
                # Video is a livestream currently broadcasting.
                # Re-queue with delayed queued_at so it retries after stream ends.
                # Calculate retry time: use scheduled_start if available, else now + 1 hour.
                if scheduled_start:
                    try:
                        stream_end = datetime.fromisoformat(scheduled_start.replace('Z', '+00:00'))
                        # Add 1 hour for transcription to complete
                        retry_time = stream_end + timedelta(hours=1)
                    except (ValueError, TypeError):
                        retry_time = datetime.now(timezone.utc) + timedelta(hours=1)
                else:
                    retry_time = datetime.now(timezone.utc) + timedelta(hours=1)
                retry_queued_at = retry_time.isoformat().replace('+00:00', 'Z')
                _requeue_video(
                    video_id=video_id,
                    channel_id=channel_id or feed["channel_id"],
                    title=video_title,
                    date=video_date,
                    scheduled_start=scheduled_start,
                    queued_at=retry_queued_at,
                    raw_entry_xml=raw_entry_xml,
                )
                logger.info(f"{channel_name}: [{video_id}] {video_title}: TubeNews: Livestream detected — re-queued for {retry_queued_at}")
                return "skipped", 0
            if transcript_rate_limit_event is not None and transcript_rate_limit_event.is_set():
                return "transcript_quota_exhausted", 0
            logger.info(f"{channel_name}: [{video_id}] {video_title}: Supadata: Fetch failed — will retry later")
            return "skipped", 0

        meeting_dir = feed_dir / f"{video_date}_{video_id}"
        meeting_dir.mkdir(exist_ok=True)
        (meeting_dir / "transcript.txt").write_text(transcript_text, encoding="utf-8")

    # --- Generate news stories via Gemini ---
    if ai_disabled:
        return "skipped", 0

    # Call Gemini once per focus, deduplicating stories by title across passes.
    # Track title → index so we can merge user_ids when the same story appears
    # in multiple focus passes.  Rate limiting is enforced inside call_gemini_api
    # via _gemini_rate_lock — no per-loop delay needed here.
    seen_titles: dict[str, int] = {}
    all_stories: list = []
    for focus, user_ids in focuses:
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
            call_delay=float(config.get("gemini_call_delay", 8)),
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
        write_story_files(all_stories, meeting_dir, video_id, video_date=video_date)
        metadata = {
            "video_id": video_id,
            "video_title": video_title,
            "status": "processed",
            "processed_at": now_utc_iso(),
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
        "processed_at": now_utc_iso(),
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
    *,
    forced_videos: list[VideoInfo] | None = None,
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

    *forced_videos* is an optional list of ``VideoInfo`` dicts to process.
    When provided, :func:`discover_videos` is skipped entirely and the same-day
    hold check is bypassed.  Used by the WebSub daemon to process pushed videos;
    titles and dates come directly from the hub's push payload so no extra
    YouTube request is made.

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

    if forced_videos is not None:
        # WebSub daemon path: title and date come from the hub push payload —
        # no extra YouTube request needed.
        videos_to_process = [
            v for v in forced_videos if _needs_processing(v["id"], feed_dir)
        ]
        if not videos_to_process:
            logger.info(f"{channel_name}: TubeNews: No new videos in push queue")
            return content_changed, ai_rate_limited, stories_written
        logger.info(f"{channel_name}: TubeNews: Processing {len(videos_to_process)} pushed video(s)")
        total = len(videos_to_process)
        for video_num, video_info in enumerate(videos_to_process, start=1):
            ai_disabled = ai_rate_limited or (
                ai_rate_limit_event is not None and ai_rate_limit_event.is_set()
            )
            # Extract queue entry fields if present (from WebSub daemon)
            queue_entry = video_info.get("_queue_entry", {})
            result, n = process_video(
                video_id=video_info["id"],
                video_title=video_info["title"],
                video_date=video_info["date"],
                feed=feed,
                feed_dir=feed_dir,
                supadata_client=supadata_client,
                config=config,
                ai_disabled=ai_disabled,
                video_num=video_num,
                total_videos=total,
                focuses=focuses,
                transcript_rate_limit_event=transcript_rate_limit_event,
                channel_id=queue_entry.get("channel_id", ""),
                scheduled_start=queue_entry.get("scheduled_start"),
                raw_entry_xml=queue_entry.get("raw_entry_xml", ""),
            )
            if result == "content_written":
                content_changed = True
                stories_written += n
            elif result == "ai_rate_limited":
                ai_rate_limited = True
                if ai_rate_limit_event is not None:
                    ai_rate_limit_event.set()
            elif result == "transcript_quota_exhausted":
                break
        return content_changed, ai_rate_limited, stories_written

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
                "processed_at": now_utc_iso(),
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
# Channel config — read from state/channels.json
# ---------------------------------------------------------------------------


def _read_channels() -> list[FeedConfig]:
    """Return the list of configured channels.

    Reads ``state/channels.json`` first.  Falls back to the ``feeds`` key in
    ``TubeNews.json`` for backward compatibility with installs that have not
    yet been migrated.  Returns ``[]`` when neither source can be read.
    """
    channels_file = STATE_ROOT / "channels.json"
    if channels_file.exists():
        try:
            return json.loads(channels_file.read_text())
        except Exception as exc:
            logger.warning(f"TubeNews: Could not read state/channels.json: {exc}")
    # Fallback: read feeds[] from TubeNews.json (pre-migration layout).
    try:
        return json.loads(CONFIG_FILE.read_text()).get("feeds", [])
    except Exception:
        return []


# ---------------------------------------------------------------------------
# WebSub (PubSubHubbub) — subscription lifecycle helpers
# ---------------------------------------------------------------------------

_WSB_HUB = "https://pubsubhubbub.appspot.com/subscribe"
_WSB_LEASE = 604800  # 7 days


def _wsb_topic(channel_id: str) -> str:
    """Return the YouTube Atom feed URL used as the WebSub topic for *channel_id*."""
    return f"https://www.youtube.com/xml/feeds/videos.xml?channel_id={channel_id}"


def _wsb_record_subscription(channel_id: str, callback_url: str) -> None:
    """Write or update the subscription record for *channel_id* in ``state/subscriptions.json``."""
    path = STATE_ROOT / "subscriptions.json"
    try:
        subs: dict = json.loads(path.read_text()) if path.exists() else {}
    except Exception:
        subs = {}
    subs[channel_id] = {
        "subscribed_at": now_utc_iso(),
        "lease_seconds": _WSB_LEASE,
        "callback_url": callback_url,
    }
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(subs, indent=2))
    tmp.replace(path)


def _wsb_remove_subscription(channel_id: str) -> None:
    """Remove the subscription record for *channel_id* from ``state/subscriptions.json``."""
    path = STATE_ROOT / "subscriptions.json"
    if not path.exists():
        return
    try:
        subs: dict = json.loads(path.read_text())
    except Exception:
        return
    subs.pop(channel_id, None)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(subs, indent=2))
    tmp.replace(path)


def _wsb_subscribe(channel_id: str, config: dict) -> bool:
    """Subscribe to WebSub push notifications for *channel_id*.

    POSTs to the YouTube PubSubHubbub hub requesting a 7-day lease.
    Records the subscription in ``state/subscriptions.json`` on success.

    Returns:
        ``True`` on success (HTTP 202), ``False`` on failure or when
        ``websub_callback_url`` / ``websub_secret`` are not configured.
    """
    cb = config.get("websub_callback_url", "")
    sec = config.get("websub_secret", "")
    if not cb or not sec:
        return False
    try:
        r = requests.post(_WSB_HUB, data={
            "hub.mode": "subscribe",
            "hub.topic": _wsb_topic(channel_id),
            "hub.callback": cb,
            "hub.secret": sec,
            "hub.lease_seconds": _WSB_LEASE,
        }, timeout=10)
        ok = r.status_code == 202
        if ok:
            _wsb_record_subscription(channel_id, cb)
            logger.debug(f"WebSub: subscribed channel {channel_id}")
        else:
            logger.warning(f"WebSub: subscribe returned HTTP {r.status_code} for {channel_id}")
        return ok
    except Exception as exc:
        logger.warning(f"WebSub: subscribe failed for {channel_id}: {exc}")
        return False


def _wsb_unsubscribe(channel_id: str, config: dict) -> bool:
    """Unsubscribe from WebSub push notifications for *channel_id*.

    POSTs an unsubscribe request to the hub and removes the record from
    ``state/subscriptions.json`` on success.

    Returns:
        ``True`` on success (HTTP 202), ``False`` on failure or when
        ``websub_callback_url`` / ``websub_secret`` are not configured.
    """
    cb = config.get("websub_callback_url", "")
    sec = config.get("websub_secret", "")
    if not cb or not sec:
        return False
    try:
        r = requests.post(_WSB_HUB, data={
            "hub.mode": "unsubscribe",
            "hub.topic": _wsb_topic(channel_id),
            "hub.callback": cb,
            "hub.secret": sec,
        }, timeout=10)
        ok = r.status_code == 202
        if ok:
            _wsb_remove_subscription(channel_id)
            logger.debug(f"WebSub: unsubscribed channel {channel_id}")
        else:
            logger.warning(f"WebSub: unsubscribe returned HTTP {r.status_code} for {channel_id}")
        return ok
    except Exception as exc:
        logger.warning(f"WebSub: unsubscribe failed for {channel_id}: {exc}")
        return False


# ---------------------------------------------------------------------------
# Push queue helpers — used by --daemon mode
# ---------------------------------------------------------------------------


def _read_push_queue(min_age_minutes: float) -> list[dict]:
    """Return queue entries whose ``queued_at`` timestamp is old enough to process.

    An entry is considered ripe when ``queued_at`` is at least
    *min_age_minutes* minutes in the past.  This delay lets auto-captions
    finish and live streams end before we fetch the transcript.

    Args:
        min_age_minutes: Minimum age in minutes before an entry is returned.

    Returns:
        List of ripe queue dicts, each with ``video_id``, ``channel_id``,
        and ``queued_at`` keys.  Returns ``[]`` when the queue file is absent
        or cannot be parsed.
    """
    path = STATE_ROOT / "queue" / "push_queue.json"
    if not path.exists():
        return []
    try:
        items: list[dict] = json.loads(path.read_text())
    except Exception:
        return []
    # Use is_ripe() helper which handles both ISO 8601 strings and legacy Unix floats,
    # and treats None/0 as immediate processing
    result = []
    for i in items:
        queued_at = i.get("queued_at")
        # Handle legacy format: 0 means immediate processing
        if isinstance(queued_at, (int, float)) and queued_at == 0:
            queued_at = None
        if is_ripe(queued_at, int(min_age_minutes)):
            result.append(i)
    return result


_QUEUE_MAX_RETRIES = 10


def _update_queue_retry_counts(updated_entries: list[dict]) -> None:
    """Update ``retry_count`` for queue entries that weren't resolved this cycle.

    Reads ``push_queue.json``, replaces matching entries with the updated
    versions (which carry an incremented ``retry_count``), and writes back
    atomically.  No-op when the queue file is absent.
    """
    path = STATE_ROOT / "queue" / "push_queue.json"
    if not path.exists():
        return
    try:
        items: list[dict] = json.loads(path.read_text())
    except Exception:
        return
    by_vid = {e["video_id"]: e for e in updated_entries}
    merged = [by_vid.get(i.get("video_id"), i) for i in items]
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(merged, indent=2))
    tmp.replace(path)


def _remove_from_queue(processed_ids: set[str]) -> None:
    """Remove entries for *processed_ids* from ``state/queue/push_queue.json``.

    Entries whose ``video_id`` is not in *processed_ids* are kept unchanged.
    No-op when the queue file is absent.

    Args:
        processed_ids: Set of video ID strings to remove.
    """
    path = STATE_ROOT / "queue" / "push_queue.json"
    if not path.exists():
        return
    try:
        items: list[dict] = json.loads(path.read_text())
    except Exception:
        return
    remaining = [i for i in items if i.get("video_id") not in processed_ids]
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(remaining, indent=2))
    tmp.replace(path)


def _requeue_video(
    video_id: str,
    channel_id: str,
    title: str,
    date: str,
    scheduled_start: str | None,
    queued_at: str,
    raw_entry_xml: str = "",
) -> None:
    """Re-queue a video for later processing (livestream still broadcasting).

    Updates the video's ``queued_at`` timestamp to defer processing until
    after the livestream ends. If the video is not yet in the queue, it is
    added. Existing ``retry_count`` is preserved.

    Args:
        video_id: YouTube video ID
        channel_id: YouTube channel ID
        title: Video title
        date: Video publish date (ISO 8601)
        scheduled_start: Scheduled stream start time (ISO 8601), or None
        queued_at: New timestamp for when this video becomes ripe (ISO 8601)
        raw_entry_xml: Raw Atom entry XML from WebSub notification
    """
    queue_dir = STATE_ROOT / "queue"
    queue_dir.mkdir(parents=True, exist_ok=True)
    queue_path = queue_dir / "push_queue.json"

    with queue_lock:
        try:
            items: list[dict] = json.loads(queue_path.read_text()) if queue_path.exists() else []
        except Exception:
            items = []

        # Preserve existing entry if present, only update queued_at
        by_vid = {i["video_id"]: i for i in items}
        existing_retry_count = by_vid.get(video_id, {}).get("retry_count", 0)

        entry = {
            "video_id": video_id,
            "channel_id": channel_id,
            "title": title,
            "date": date,
            "scheduled_start": scheduled_start,
            "raw_entry_xml": raw_entry_xml,
            "queued_at": queued_at,
            "retry_count": existing_retry_count,
        }
        by_vid[video_id] = entry

        updated = list(by_vid.values())
        tmp = queue_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(updated, indent=2))
        tmp.replace(queue_path)


def _recover_orphaned_videos() -> int:
    """Scan the content archive for meeting dirs that have no ``metadata.json``.

    Such directories represent videos that were downloaded (or partially
    processed) but never completed — e.g. the daemon was interrupted mid-run,
    or the operator deleted a ``metadata.json`` to force a re-run.

    Each orphaned video is added to the push queue with ``queued_at = 0`` so
    it is immediately ripe on the next processor cycle.  Videos already in the
    queue are left untouched (their existing ``queued_at`` is preserved).

    Returns the number of newly queued videos.
    """
    queue_dir = STATE_ROOT / "queue"
    queue_dir.mkdir(parents=True, exist_ok=True)
    queue_path = queue_dir / "push_queue.json"

    # Load existing queue so we don't duplicate entries
    try:
        existing: list[dict] = json.loads(queue_path.read_text()) if queue_path.exists() else []
    except Exception:
        existing = []
    already_queued: set[str] = {e.get("video_id", "") for e in existing}

    new_entries: list[dict] = []

    for channel_dir in sorted(STORAGE_ROOT.iterdir()):
        if not channel_dir.is_dir() or channel_dir.name.startswith("_"):
            continue
        channel_json = channel_dir / "channel.json"
        if not channel_json.exists():
            continue
        try:
            cinfo = json.loads(channel_json.read_text())
        except Exception:
            continue
        channel_id = cinfo.get("channel_id", "")
        if not channel_id:
            continue

        for meeting_dir in sorted(channel_dir.iterdir()):
            if not meeting_dir.is_dir():
                continue
            if (meeting_dir / "metadata.json").exists():
                continue
            # Directory name format: YYYY-MM-DD_videoId
            name = meeting_dir.name
            parts = name.split("_", 1)
            if len(parts) != 2:
                continue
            video_id = parts[1]
            if not video_id or video_id in already_queued:
                continue
            new_entries.append({
                "video_id":   video_id,
                "channel_id": channel_id,
                "title":      "",
                "date":       parts[0],
                "queued_at":  None,
            })
            already_queued.add(video_id)

    if new_entries:
        by_vid = {e["video_id"]: e for e in existing}
        for ne in new_entries:
            by_vid[ne["video_id"]] = ne
        tmp = queue_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(list(by_vid.values()), indent=2))
        tmp.replace(queue_path)
        ids = ", ".join(e["video_id"] for e in new_entries)
        logger.info(f"Orphan recovery: queued {len(new_entries)} video(s): {ids}")

    return len(new_entries)


# ---------------------------------------------------------------------------
# --daemon mode — WebSub receiver + processor threads
# ---------------------------------------------------------------------------

import hashlib as _hashlib
import hmac as _hmac
import http.server as _http_server


def _wsb_receiver_thread(config: dict) -> None:
    """Thread 1: HTTP server that receives and validates WebSub push payloads.

    Listens on ``0.0.0.0:{websub_daemon_port}`` (default 8675).

    * ``GET /`` — hub subscription verification: checks that ``hub.topic``
      matches a known channel feed URL, then echoes back ``hub.challenge``.
    * ``POST /`` — push payload: verifies the HMAC-SHA1 ``X-Hub-Signature``
      header, parses the Atom XML for ``yt:videoId`` and ``yt:channelId``,
      and writes (or updates) an entry in ``state/queue/push_queue.json``.

    Runs until the process exits (daemon thread).
    """
    port = int(config.get("websub_daemon_port", 8675))
    secret = config.get("websub_secret", "").encode()
    channels = _read_channels()
    known_topics = {_wsb_topic(ch["channel_id"]): ch["channel_id"] for ch in channels}

    queue_dir = STATE_ROOT / "queue"
    queue_dir.mkdir(parents=True, exist_ok=True)
    queue_path = queue_dir / "push_queue.json"
    queue_lock = threading.Lock()

    _YT_NS = {
        "atom": "http://www.w3.org/2005/Atom",
        "yt":   "http://www.youtube.com/xml/schemas/2015",
    }

    class _Handler(_http_server.BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):  # silence access log
            logger.debug("WebSub receiver: " + fmt % args)

        def do_GET(self):
            from urllib.parse import urlparse, parse_qs
            qs = parse_qs(urlparse(self.path).query)
            topic = (qs.get("hub.topic") or [""])[0]
            challenge = (qs.get("hub.challenge") or [""])[0]
            if topic not in known_topics or not challenge:
                self.send_response(404)
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(challenge.encode())
            logger.info(f"WebSub: verified subscription for channel {known_topics[topic]}")

        def do_POST(self):
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length)

            sig_header = self.headers.get("X-Hub-Signature", "")
            if secret and sig_header.startswith("sha1="):
                expected = _hmac.new(secret, body, _hashlib.sha1).hexdigest()
                if not _hmac.compare_digest(sig_header[5:], expected):
                    self.send_response(403)
                    self.end_headers()
                    logger.warning("WebSub: rejected push — HMAC mismatch")
                    return

            try:
                root = ET.fromstring(body)
            except ET.ParseError:
                self.send_response(400)
                self.end_headers()
                return

            now = now_utc_iso()
            new_entries: list[dict] = []
            for entry in root.findall("atom:entry", _YT_NS):
                vid_el   = entry.find("yt:videoId",    _YT_NS)
                ch_el    = entry.find("yt:channelId",  _YT_NS)
                title_el = entry.find("atom:title",    _YT_NS)
                pub_el   = entry.find("atom:published", _YT_NS)
                sched_el = entry.find("yt:scheduledStartTime", _YT_NS)
                if vid_el is not None and ch_el is not None:
                    pub_raw = (pub_el.text or "").strip() if pub_el is not None else ""
                    pub_date = pub_raw  # full ISO 8601 timestamp
                    sched_start = (sched_el.text or "").strip() if sched_el is not None else None
                    # Preserve complete entry for future metadata extraction
                    raw_entry = ET.tostring(entry, encoding='unicode')
                    new_entries.append({
                        "video_id":   vid_el.text.strip(),
                        "channel_id": ch_el.text.strip(),
                        "title":      (title_el.text or "").strip() if title_el is not None else "",
                        "date":       pub_date,
                        "scheduled_start": sched_start,
                        "raw_entry_xml": raw_entry,
                        "queued_at":  now,
                    })

            if new_entries:
                with queue_lock:
                    try:
                        existing: list[dict] = (
                            json.loads(queue_path.read_text()) if queue_path.exists() else []
                        )
                    except Exception:
                        existing = []
                    by_vid = {e["video_id"]: e for e in existing}
                    for ne in new_entries:
                        by_vid[ne["video_id"]] = ne  # keep latest queued_at
                    updated = list(by_vid.values())
                    tmp = queue_path.with_suffix(".tmp")
                    tmp.write_text(json.dumps(updated, indent=2))
                    tmp.replace(queue_path)
                ids = ", ".join(e["video_id"] for e in new_entries)
                logger.info(f"WebSub: queued {len(new_entries)} video(s): {ids}")

            self.send_response(204)
            self.end_headers()

    server = _http_server.HTTPServer(("0.0.0.0", port), _Handler)
    logger.info(f"WebSub: receiver listening on 0.0.0.0:{port}")
    server.serve_forever()


def _wsb_processor_thread(config: dict) -> None:
    """Thread 2: periodically checks the push queue and processes ripe entries.

    On each wake-up:

    1. **Config reload:** checks TubeNews.json for changes and applies them
       (daemon config is reloaded from disk on each cycle).
    2. **Renewal check:** re-subscribes any channel whose WebSub lease expires
       within the next 24 hours.
    3. **Orphan recovery (once per day):** scans the content archive for meeting
       directories without ``metadata.json`` and queues them for processing.
    4. **Queue processing:** reads ripe entries (older than
       ``websub_min_age_minutes``), acquires the run lock, calls
       :func:`process_feed` for each affected channel, rebuilds the aggregate
       feed if anything changed, then removes processed entries from the queue.

    Sleep interval is ``websub_check_interval_minutes`` (default 10 minutes).
    Runs until the process exits (daemon thread).
    """
    # When Gemini returns 429, back off for this many seconds before retrying
    # AI calls.  Videos stay in the queue; only the AI step is skipped.
    _AI_BACKOFF_SECONDS = 3600  # 1 hour
    _ai_backoff_until: float = 0.0

    # Run orphan recovery immediately on startup, then once per 24 h.
    try:
        _recover_orphaned_videos()
    except Exception as exc:
        logger.warning(f"WebSub processor: orphan recovery failed: {exc}")
    _last_orphan_recovery: float = time.time()

    while True:
        # -- Config reload ----------------------------------------------------
        _reload_config_from_disk()

        # Read current values from reloadable config
        with _config_lock:
            interval = float(_daemon_config.get("websub_check_interval_minutes", 10)) * 60
            min_age = float(_daemon_config.get("websub_min_age_minutes", 360))
            supadata_key = _daemon_config.get("supadata_api_key")
        supadata_client = Supadata(api_key=supadata_key)
        # -- Renewal check ----------------------------------------------------
        subs_path = STATE_ROOT / "subscriptions.json"
        if subs_path.exists():
            try:
                subs: dict = json.loads(subs_path.read_text())
            except Exception:
                subs = {}
            renew_before = time.time() + 86400  # within next 24 h
            for cid, info in subs.items():
                subscribed_at = _get_timestamp_as_float(info.get("subscribed_at", 0))
                expires = subscribed_at + info.get("lease_seconds", _WSB_LEASE)
                if expires <= renew_before:
                    logger.info(f"WebSub: renewing subscription for channel {cid}")
                    _wsb_subscribe(cid, config)

        # -- Orphan recovery (once per 24 h) ----------------------------------
        if time.time() - _last_orphan_recovery >= 86400:
            try:
                _recover_orphaned_videos()
            except Exception as exc:
                logger.warning(f"WebSub processor: orphan recovery failed: {exc}")
            _last_orphan_recovery = time.time()

        # -- Queue processing -------------------------------------------------
        ripe = _read_push_queue(min_age)
        if not ripe:
            continue

        # Cap how many videos are processed per cycle so we don't burst-call
        # Gemini with the entire backlog at once.  Remaining entries stay ripe
        # and are picked up next cycle.  Default: 3 videos per cycle.
        max_per_cycle = int(config.get("websub_max_videos_per_cycle", 3))
        if len(ripe) > max_per_cycle:
            logger.info(
                f"WebSub processor: {len(ripe)} ripe entries — "
                f"processing {max_per_cycle} this cycle, {len(ripe) - max_per_cycle} deferred"
            )
            ripe = ripe[:max_per_cycle]

        if not _acquire_lock():
            logger.debug("WebSub processor: lock held by another process — skipping this cycle")
            continue

        try:
            channels = _read_channels()
            channel_map = {ch["channel_id"]: ch for ch in channels}

            by_channel: dict[str, list[dict]] = {}
            today_str = date.today().isoformat()
            for entry in ripe:
                cid = entry.get("channel_id", "")
                vid = entry.get("video_id", "")
                if cid and vid and cid in channel_map:
                    video_info = {
                        "id":    vid,
                        "title": entry.get("title", "") or "[title unknown]",
                        "date":  entry.get("date", "") or today_str,
                        # Preserve full queue entry fields for process_video
                        "_queue_entry": entry,
                    }
                    by_channel.setdefault(cid, []).append(video_info)

            if not by_channel:
                _remove_from_queue({e["video_id"] for e in ripe})
                continue

            ai_in_backoff = time.time() < _ai_backoff_until
            if ai_in_backoff:
                remaining = int(_ai_backoff_until - time.time())
                logger.info(
                    f"WebSub processor: Gemini backoff active — skipping AI for "
                    f"this cycle ({remaining}s remaining)"
                )
            ai_event = threading.Event()
            if ai_in_backoff:
                ai_event.set()  # pre-set so process_feed skips AI immediately
            transcript_event = threading.Event()
            any_changed = False

            for cid, video_infos in by_channel.items():
                feed = channel_map[cid]
                content_changed, ai_rate_limited, _ = process_feed(
                    feed, supadata_client, config,
                    ai_event, transcript_event,
                    forced_videos=video_infos,
                )
                if content_changed:
                    rebuild_feed(STORAGE_ROOT / slugify(feed["channel_name"]), feed)
                    any_changed = True
                if ai_rate_limited and not ai_in_backoff:
                    _ai_backoff_until = time.time() + _AI_BACKOFF_SECONDS
                    logger.warning(
                        f"WebSub processor: Gemini rate-limited — backing off "
                        f"AI calls for {_AI_BACKOFF_SECONDS // 60} minutes"
                    )

            if any_changed or not (STORAGE_ROOT / "rss.xml").exists():
                try:
                    with _config_lock:
                        base_url = _daemon_config.get("base_url", "")
                    rebuild_aggregate_feed(base_url=base_url)
                except Exception:
                    logger.warning("WebSub processor: aggregate feed rebuild failed")

            # Only remove videos that were permanently resolved (metadata.json
            # now exists).  Unresolved items (AI rate-limited, transcript not
            # ready yet) stay in the queue and are retried next cycle.
            # Cap retries at _QUEUE_MAX_RETRIES to avoid queue bloat.
            resolved_ids: set[str] = set()
            retry_updates: list[dict] = []
            for entry in ripe:
                vid = entry.get("video_id", "")
                cid = entry.get("channel_id", "")
                date_str = entry.get("date", "")

                # Skip if video's publish date is still in the future
                if date_str:
                    try:
                        from datetime import datetime as _dt_check
                        pub_time = _dt_check.fromisoformat(date_str.replace('Z', '+00:00')).timestamp()
                        if pub_time > time.time():
                            logger.debug(
                                f"WebSub processor: {vid} publish date is in future; "
                                f"skipping until {date_str}"
                            )
                            continue
                    except (ValueError, TypeError):
                        pass  # Malformed date; proceed with normal logic

                feed_cfg = channel_map.get(cid)
                if not feed_cfg:
                    resolved_ids.add(vid)
                    continue
                feed_dir_q = STORAGE_ROOT / slugify(feed_cfg["channel_name"])
                if not _needs_processing(vid, feed_dir_q):
                    resolved_ids.add(vid)
                else:
                    retry_count = entry.get("retry_count", 0) + 1
                    if retry_count > _QUEUE_MAX_RETRIES:
                        logger.warning(
                            f"WebSub processor: dropping {vid} after "
                            f"{_QUEUE_MAX_RETRIES} failed retries"
                        )
                        resolved_ids.add(vid)
                    else:
                        retry_updates.append({**entry, "retry_count": retry_count})

            _remove_from_queue(resolved_ids)
            if retry_updates:
                _update_queue_retry_counts(retry_updates)
            kept = len(retry_updates)
            done = len(resolved_ids)
            logger.info(
                f"WebSub processor: resolved {done} video(s)"
                + (f", kept {kept} for retry" if kept else "")
            )
        finally:
            _release_lock()

        time.sleep(interval)


# ---------------------------------------------------------------------------
# --daemon mode — config reloading
# ---------------------------------------------------------------------------

_daemon_config: dict = {}
_config_lock = threading.RLock()
_config_mtime: float = 0.0  # Last known mtime of TubeNews.json


def _reload_config_from_disk() -> dict:
    """Reload TubeNews.json and atomically update daemon config.

    Only updates values that changed. Logs all changes. On error, keeps old
    values and returns existing config. Uses mtime check to avoid parsing
    unchanged files — only reloads if TubeNews.json has been modified.

    Returns: The updated _daemon_config dict (same reference).
    """
    global _daemon_config, _config_mtime

    config_file = Path(__file__).parent / "TubeNews.json"

    # Fast gate: check if file has been modified since last reload
    try:
        current_mtime = config_file.stat().st_mtime
        if current_mtime == _config_mtime and _config_mtime > 0:
            # File unchanged — skip everything
            return _daemon_config
    except (FileNotFoundError, OSError):
        # Can't stat file — fall through to read and handle error normally
        current_mtime = 0.0

    # Try to read fresh config from disk
    try:
        fresh: dict = json.loads(config_file.read_text(encoding="utf-8"))
    except FileNotFoundError:
        logger.warning("Config reload: TubeNews.json not found — keeping old config")
        return _daemon_config
    except json.JSONDecodeError as exc:
        logger.warning(f"Config reload: failed to parse TubeNews.json: {exc} — keeping old config")
        return _daemon_config
    except Exception as exc:
        logger.warning(f"Config reload: failed to read TubeNews.json: {exc} — keeping old config")
        return _daemon_config

    # Validate required keys
    missing = []
    for key in ["gemini_api_key", "supadata_api_key"]:
        if key not in fresh:
            missing.append(key)
    if missing:
        logger.warning(f"Config reload: missing required keys {missing} — keeping old config")
        return _daemon_config

    # Atomically update config with change detection
    with _config_lock:
        # Track changes for logging
        changed = {}
        immutable_attempted = {}

        # Keys that can change at runtime
        mutable_keys = {
            "gemini_api_key",
            "supadata_api_key",
            "request_timeout",
            "gemini_call_delay",
            "gemini_model",
            "base_url",
            "ntfy_topic",
            "websub_check_interval_minutes",
            "websub_min_age_minutes",
            "websub_max_videos_per_cycle",
        }

        # Keys that shouldn't change (but warn if they do)
        immutable_keys = {
            "websub_daemon_port",
            "websub_secret",
            "websub_callback_url",
        }

        for key in mutable_keys:
            if key in fresh:
                old_val = _daemon_config.get(key)
                new_val = fresh[key]
                if old_val != new_val:
                    changed[key] = (old_val, new_val)
                    _daemon_config[key] = new_val

        # Warn about immutable key changes
        for key in immutable_keys:
            if key in fresh:
                old_val = _daemon_config.get(key)
                new_val = fresh[key]
                if old_val != new_val:
                    immutable_attempted[key] = (old_val, new_val)

        # Special handling: apply request_timeout immediately
        if "request_timeout" in changed:
            old, new = changed["request_timeout"]
            try:
                socket.setdefaulttimeout(float(new))
                logger.info(
                    f"Config reload: request_timeout {old} → {new} (applied immediately)"
                )
            except (ValueError, TypeError) as exc:
                logger.error(
                    f"Config reload: invalid request_timeout value {new}: {exc} — keeping {old}"
                )
                _daemon_config["request_timeout"] = old
                del changed["request_timeout"]

        # Log all other changes (mask sensitive values)
        for key, (old, new) in changed.items():
            if key != "request_timeout":
                if "api_key" in key or "secret" in key:
                    old_display = f"***{str(old)[-4:]}" if old else "***"
                    new_display = f"***{str(new)[-4:]}" if new else "***"
                else:
                    old_display, new_display = old, new
                logger.info(f"Config reload: {key} {old_display} → {new_display}")

        # Warn about immutable key changes
        for key, (old, new) in immutable_attempted.items():
            logger.warning(
                f"Config reload: {key} cannot be changed at runtime "
                f"({old} → {new}) — requires restart to take effect"
            )

    if not changed:
        logger.debug("Config reload: no changes detected")

    # Update mtime to skip checking this file until it's modified again
    if current_mtime > 0:
        _config_mtime = current_mtime

    return _daemon_config


def _run_daemon(config: dict) -> None:
    """Start the WebSub daemon: subscribe all channels, then run the two threads.

    Thread 1 receives HTTP push payloads from YouTube's hub.
    Thread 2 wakes up periodically to process ripe queue entries and renew
    subscriptions.  Both are daemon threads — they exit when the process exits.

    This function blocks indefinitely (joins Thread 2).
    """
    global _daemon_config
    _daemon_config = config.copy()

    channels = _read_channels()
    if not channels:
        logger.error("TubeNews daemon: no channels configured — nothing to subscribe to.")
        return

    logger.info(f"TubeNews daemon: subscribing {len(channels)} channel(s) to WebSub...")
    for ch in channels:
        ok = _wsb_subscribe(ch["channel_id"], config)
        status = "OK" if ok else "skipped (not configured or failed)"
        logger.info(f"  {ch['channel_name']}: {status}")

    t1 = threading.Thread(target=_wsb_receiver_thread, args=(config,), daemon=True)
    t2 = threading.Thread(target=_wsb_processor_thread, args=(config,), daemon=True)
    t1.start()
    t2.start()
    logger.info("TubeNews daemon running. Press Ctrl+C to stop.")
    t2.join()


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

LOCK_FILE = STATE_ROOT / ".tubenews.lock"


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
    parser.add_argument(
        "--single-run",
        action="store_true",
        help="Run once and exit (process all channels immediately). Default: run as WebSub daemon",
    )
    args = parser.parse_args()

    setup_logging(args.debug)

    # Default is daemon mode; --single-run for one-time processing
    if not args.single_run:
        try:
            with open(CONFIG_FILE, "r") as f:
                config = json.load(f)
        except Exception as exc:
            logger.error(f"TubeNews daemon: could not load config: {exc}")
            return
        _setup_daemon_logging()
        logger.info("TubeNews daemon starting in WebSub mode...")
        _run_daemon(config)
        return

    if not _acquire_lock():
        logger.error("TubeNews: Another instance is already running. Exiting.")
        return

    try:
        _add_run_log_file_handler()
        _main_body(args)
    finally:
        _release_lock()


def _main_body(args) -> None:
    """Core run logic, called from main() after the lock is acquired."""
    with open(CONFIG_FILE, "r") as config_file:
        config = json.load(config_file)

    channels = _read_channels()
    if not channels:
        logger.error("TubeNews: No channels configured in state/channels.json — nothing to do.")
        return

    # Reject duplicate channel_ids before spawning threads — two entries for
    # the same channel would race to process the same video directories.
    seen_ids: dict[str, str] = {}
    for feed in channels:
        cid = feed.get("channel_id", "")
        cname = feed.get("channel_name", "?")
        if cid in seen_ids:
            logger.error(
                f"TubeNews: Duplicate channel_id '{cid}' in feeds "
                f"('{seen_ids[cid]}' and '{cname}'). "
                "Fix state/channels.json and re-run."
            )
            return
        seen_ids[cid] = cname

    supadata_client = Supadata(api_key=config["supadata_api_key"])
    logger.info(f"Session Start | {_fmt_no_leading_zeros(datetime.now(), '%A, %B %d, %Y')} | AI Model: {config.get('gemini_model')}")

    # Check cached Supadata balance before doing any work.
    quota_ok, cached_balance = _check_supadata_quota(config)
    started_at = now_utc_iso()
    if not quota_ok:
        run_log_path = STATE_ROOT / "run_logs" / "run_log.json"
        run_log_path.parent.mkdir(exist_ok=True)
        try:
            runs = json.loads(run_log_path.read_text()) if run_log_path.exists() else []
        except Exception:
            runs = []
        runs.append({
            "started_at": started_at,
            "finished_at": now_utc_iso(),
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

    max_workers = min(len(channels), config.get("max_parallel_feeds", 1))
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        feed_results = list(executor.map(_run_feed, channels))
    total_stories = sum(r["stories_written"] for r in feed_results)

    if any_content_changed.is_set() or not (STORAGE_ROOT / "rss.xml").exists():
        try:
            rebuild_aggregate_feed(base_url=config.get("base_url", ""))
        except Exception:
            logger.warning("TubeNews: Meta feed rebuild failed — skipping; user feeds will still be rebuilt")

    users_dir = STATE_ROOT / "users"
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

    run_log_path = STATE_ROOT / "run_logs" / "run_log.json"
    run_log_path.parent.mkdir(exist_ok=True)
    try:
        runs = json.loads(run_log_path.read_text()) if run_log_path.exists() else []
    except Exception as exc:
        logger.warning(f"Failed to load run log; starting fresh: {exc}")
        runs = []
    runs.append({
        "started_at": started_at,
        "finished_at": now_utc_iso(),
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
    (``state/supadata_balance.json``) — no live API call is made here so no
    credits are consumed.  If the file is absent (first run) we proceed
    optimistically; the end-of-run cache will populate it for next time.

    Returns:
        ``(ok, balance)`` — *ok* is True when there are credits remaining
        (or when the balance cannot be determined), *balance* is the raw
        cached dict or None.  When *ok* is False the caller should abort
        and record ``transcript_quota_exhausted`` in the run log.
    """
    balance_path = STATE_ROOT / "supadata_balance.json"
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
    """Fetch Supadata credit usage and cache it to ``state/supadata_balance.json``.

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
            (STATE_ROOT / "supadata_balance.json").write_text(
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
