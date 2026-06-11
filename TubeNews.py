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
import base64
from concurrent.futures import ThreadPoolExecutor
import hashlib
import html
import json
import logging
import os
import re
import signal
import socket
import threading
import time
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator, NotRequired, TypedDict

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


def _validate_config(config: dict) -> None:
    """Validate required config keys exist and have valid values.

    Raises ValueError with a clear error message if validation fails.
    This prevents confusing KeyError crashes later.

    Args:
        config: Loaded TubeNews.json configuration dict.

    Raises:
        ValueError: If required keys are missing or invalid.
    """
    required_keys = {
        "gemini_api_key": "Google Gemini API key (get from https://aistudio.google.com)",
        "gemini_model": "Gemini model name (e.g., 'gemini-2.5-flash')",
        "supadata_api_key": "Supadata API key (get from https://supadata.ai)",
    }

    missing = []
    for key, description in required_keys.items():
        if key not in config or not config[key]:
            missing.append(f"  • {key}: {description}")

    if missing:
        raise ValueError(
            f"TubeNews: Missing required configuration keys in {CONFIG_FILE}:\n"
            + "\n".join(missing) + "\n"
            "Copy TubeNews.json.sample and fill in your API keys."
        )

    # Validate API keys are strings and non-empty
    for key in required_keys:
        if not isinstance(config[key], str) or not config[key].strip():
            raise ValueError(
                f"TubeNews: {key} must be a non-empty string in {CONFIG_FILE}"
            )


def _get_config_safe(config: dict, key: str, default: object) -> object:
    """Safely get a config value with a default and type conversion.

    Args:
        config: Configuration dict.
        key: Key to retrieve.
        default: Value to return if key is missing or invalid.

    Returns:
        Value from config or default.
    """
    try:
        val = config.get(key)
        if val is None:
            return default
        return val
    except Exception:
        return default


def _safe_int(val: Any, default: int) -> int:
    """Safely convert a value to int with a default fallback.

    Args:
        val: Value to convert.
        default: Default if conversion fails.

    Returns:
        Converted int or default.
    """
    try:
        return int(val) if val is not None else default
    except (ValueError, TypeError):
        return default


def _safe_float(val: Any, default: float) -> float:
    """Safely convert a value to float with a default fallback.

    Args:
        val: Value to convert.
        default: Default if conversion fails.

    Returns:
        Converted float or default.
    """
    try:
        return float(val) if val is not None else default
    except (ValueError, TypeError):
        return default


def _atomic_write(path: Path, content: str) -> None:
    """Atomically write content to a file using write-then-rename.

    Ensures that if the process crashes or fails during write, the original
    file is not corrupted. On failure, raises an exception and cleans up the
    temporary file.

    Args:
        path: Path to write to (e.g., /var/data/file.json).
        content: String content to write.

    Raises:
        Exception: On write or rename failure (cleaned up).
    """
    tmp = path.with_suffix(".tmp")
    try:
        tmp.write_text(content)
        tmp.replace(path)
    except Exception as exc:
        # Clean up temporary file if it exists
        try:
            tmp.unlink(missing_ok=True)
        except Exception:
            pass
        raise exc


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
    published_at: NotRequired[str]  # full ISO 8601 timestamp; "" when unavailable


class FeedConfig(TypedDict):
    """Per-channel configuration block from ``TubeNews.json``."""
    channel_id: str
    channel_name: str
    focus: str


class _GeminiStoryBase(TypedDict, total=False):
    # Internal key set by process_video before write_story_files; not returned
    # by Gemini and not stored to disk.
    _user_ids: list[str]


class GeminiStory(_GeminiStoryBase):
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
    video_published: str  # when the YouTube video was published; "" for older stories


class MetadataDict(TypedDict, total=False):
    """Contents of a ``metadata.json`` archive file.

    All fields are optional (``total=False``) because metadata files may be
    written incrementally and old files pre-date several keys.
    """
    video_id: str
    video_title: str
    video_date: str
    video_published_at: str  # full ISO 8601 publish timestamp; "" when unknown
    status: str
    processed_at: str
    skip_reason: str
    processed_focuses: list[str]


class FeedResult(TypedDict):
    """Per-channel result dict collected by ``_main_body``."""
    channel_id: str
    channel_name: str
    stories_written: int


# ---------------------------------------------------------------------------
# Utility helpers
# ---------------------------------------------------------------------------


from tubenews_utils import sanitize_focus, slugify  # noqa: E402  (below module-level constants)


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
        and not l.startswith("Video published")
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

    vid_pub_match = re.search(r"^Video published\s+(.+)$", text, re.MULTILINE)
    video_published = vid_pub_match.group(1).strip() if vid_pub_match else ""

    return {
        "title": title,
        "dateline": dateline,
        "body_html": body_html,
        "start_seconds": start_seconds,
        "topics": topics,
        "user_ids": user_ids,
        "published": published,
        "video_published": video_published,
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
# Input validation & sanitization
# ---------------------------------------------------------------------------


def _validate_video_id(video_id: str) -> bool:
    """Check if a video_id is safe (no path traversal, minimal length).

    Real YouTube IDs are 11 alphanumeric characters, but test code uses various
    formats. Validation here prevents directory traversal attacks (e.g., "..",
    "/", etc.) rather than enforcing strict format. Proper format validation
    should happen at YouTube API boundaries.

    Args:
        video_id: String to validate.

    Returns:
        True if valid, False otherwise.
    """
    if not video_id or len(video_id) < 3:
        return False
    # Reject paths with .. or / or \ which would cause traversal
    if ".." in video_id or "/" in video_id or "\\" in video_id:
        return False
    # Allow alphanumeric, hyphen, underscore
    return all(c.isalnum() or c in "-_" for c in video_id)


def _validate_channel_id(channel_id: str) -> bool:
    """Check if a channel_id is safe (no path traversal, reasonable length).

    Real YouTube channel IDs start with UC and are ~24 characters total, but
    test code uses various formats. Validation here prevents directory traversal
    attacks rather than enforcing strict YouTube format. Proper format validation
    should happen at YouTube API boundaries.

    Args:
        channel_id: String to validate.

    Returns:
        True if valid, False otherwise.
    """
    if not channel_id or len(channel_id) < 3:
        return False
    # Reject paths with .. or / or \ which would cause traversal
    if ".." in channel_id or "/" in channel_id or "\\" in channel_id:
        return False
    # Allow alphanumeric (UC* format is nice but not required for tests)
    return all(c.isalnum() or c in "-_" for c in channel_id)


# Canonical implementation lives in tubenews_utils.sanitize_focus (ASCII-only
# regex prevents Unicode homoglyph injection).  Local alias preserves the
# private naming convention used throughout this file.
_sanitize_focus = sanitize_focus


def _validate_iso_date(date_str: str) -> bool:
    """Check if a string is a valid ISO 8601 date (YYYY-MM-DD).

    Args:
        date_str: String to validate.

    Returns:
        True if valid, False otherwise.
    """
    try:
        datetime.strptime(date_str, "%Y-%m-%d")
        return True
    except (ValueError, TypeError):
        return False


# ---------------------------------------------------------------------------
# YouTube data-gathering
# ---------------------------------------------------------------------------

_YT_RSS_NS = {
    "atom": "http://www.w3.org/2005/Atom",
    "yt":   "http://www.youtube.com/xml/schemas/2015",
}
# Alias used by WebSub entry parsing helpers (same namespaces, different context).
_YT_NS = _YT_RSS_NS

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

# Global lock for WebSub push queue access (shared by _wsb_receiver_thread and _requeue_video)
_queue_lock = threading.Lock()


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


def _format_video_metadata(video_id: str, feed_name: str, video_title: str) -> str:
    """Format video metadata for logging: VideoID: Channel: Title"""
    return f"{video_id}: {feed_name}: {video_title}"


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
                logger.error(f"{prefix}YouTube RSS: Failed to fetch after 3 attempts: {exc}")
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
        # Extract just the date part (YYYY-MM-DD) from ISO timestamp
        date = pub.split("T")[0] if pub else datetime.now().strftime("%Y-%m-%d")
        videos.append({
            "id":    vid_el.text.strip(),
            "title": (title_el.text or "").strip() if title_el is not None else "",
            "date":  date,
            "published_at": pub,  # full ISO 8601 timestamp; "" if not present
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
    # Format metadata as: VideoID: Channel: Title
    metadata = _format_video_metadata(video_id, feed_name, video_title)

    url = f"https://www.youtube.com/watch?v={video_id}"
    try:
        try:
            transcript_response = supadata_client.transcript(url=url, lang="en", text=False)
        except TypeError as te:
            # Supadata library error: likely a version mismatch in error constructor
            # e.g., "SupadataError.__init__() got an unexpected keyword argument 'type'"
            if "SupadataError" in str(te) and "__init__" in str(te):
                logger.error(f"Supadata: Library error (version mismatch?) - {metadata}: {te}")
                return None
            raise
        if hasattr(transcript_response, "content") and transcript_response.content:
            segments = transcript_response.content
            lang_received = getattr(transcript_response, "lang", "") or ""
            if lang_received and lang_received != "en":
                logger.warning(f"Supadata: Requested English transcript but received '{lang_received}' - {metadata}")
            else:
                logger.debug(f"Supadata: Language: {lang_received or 'unknown'} - {metadata}")
            lines = [
                f"{int(getattr(seg, 'offset', 0) / 1000)}s --> {getattr(seg, 'text', '')}"
                for seg in segments
            ]
            transcript_text = "\n".join(lines)
            logger.info(
                f"Supadata: Transcript ready — {len(segments)} segments, {len(transcript_text):,} chars - {metadata}"
            )
            return transcript_text
        # API returned a response but no transcript content — video has no captions.
        logger.info(f"Supadata: No transcript content returned - {metadata}")
        if failure_reason is not None:
            failure_reason.append("no_captions")
        return False
    except Exception as exc:
        exc_str = str(exc).lower()
        # Detect quota / credit exhaustion from SupadataError or HTTP 402/429.
        is_quota_error = False
        http_status = None
        if isinstance(exc, SupadataError):
            is_quota_error = any(
                kw in (exc.error or "").lower()
                for kw in ("credit", "quota", "payment", "limit", "billing")
            )
        elif isinstance(exc, requests.exceptions.HTTPError):
            status = getattr(getattr(exc, "response", None), "status_code", None)
            http_status = status
            is_quota_error = status == 402

        # Detect service unavailability (5xx errors) — don't give up after 12 hours
        is_service_error = http_status in (500, 502, 503, 504)

        # Detect permanent "no transcript" from SupadataError codes.
        is_permanent_no_transcript = isinstance(exc, SupadataError) and (
            exc.error == "transcript-unavailable"
            or exc.error == "forbidden"
            or "not-found" in (exc.error or "")
        )

        if is_quota_error:
            logger.error(
                f"Supadata: Quota exhausted — no credits remaining. "
                f"Halting transcript fetches for this cycle - {metadata}"
            )
            if transcript_rate_limit_event is not None:
                transcript_rate_limit_event.set()
        elif is_service_error:
            logger.warning(f"Supadata: Service error (HTTP {http_status}) — will retry - {metadata}")
        elif is_permanent_no_transcript:
            error_code = getattr(exc, "error", "") or ""
            if "forbidden" in error_code:
                reason = "members_only_or_restricted"
            elif "not-found" in error_code:
                reason = "video_not_found"
            else:
                reason = "no_captions"
            logger.info(f"Supadata: No transcript available ({reason}) - {metadata}")
            if failure_reason is not None:
                failure_reason.append(reason)
            return False
        elif "live streaming" in exc_str:
            logger.warning(f"Supadata: Live stream — transcript unavailable, will retry later - {metadata}")
            if livestream_error is not None:
                livestream_error.append(True)
        else:
            logger.error(f"Supadata: Call failed - {metadata}: {exc}")

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
    video_id: str = "",
    call_delay: float = 8.0,
) -> list[GeminiStory] | bool | str | None:
    """Send a transcript to Google Gemini and parse the returned news stories.

    The prompt instructs Gemini to act as an investigative reporter and return
    a JSON list of story objects.  We ask for raw JSON (no markdown fences) so
    it can be parsed directly.

    Returns:
        list  – one dict per story on success.
        False – API returned HTTP 429 (quota exhausted); caller should back off.
        "service_unavailable" – API returned HTTP 503 (service down); caller should back off longer.
        None  – any other failure; the caller should skip this video.
    """
    # Format metadata as: VideoID: Channel: Title
    metadata = _format_video_metadata(video_id, feed_name, video_title)

    # Sanitize user input to prevent prompt injection
    focus = _sanitize_focus(focus)
    global _gemini_last_call_time
    with _gemini_rate_lock:
        elapsed = time.time() - _gemini_last_call_time
        if elapsed < call_delay:
            time.sleep(call_delay - elapsed)
        _gemini_last_call_time = time.time()

    api_version = "v1beta"  # v1beta supports all models; no need to guess
    api_url = (
        f"https://generativelanguage.googleapis.com/{api_version}/models/"
        f"{model_name}:generateContent?key={gemini_api_key}"
    )

    directive = (
        "You are a highly experienced investigative reporter. "
        f"Analyze this transcript of '{video_title}' recorded on {video_date}.\n\n"
        "OBJECTIVE: Identify and extract distinct news stories strictly "
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

    try:
        response = requests.post(api_url, json=payload, timeout=_GEMINI_TIMEOUT)
        if response.status_code == 200:
            # Safely extract response text with bounds checking
            try:
                resp_json = response.json()
                if not isinstance(resp_json, dict):
                    logger.error(
                        f"Gemini: Invalid response format - {metadata}"
                        f" (expected dict, got {type(resp_json).__name__})"
                    )
                    return None

                candidates = resp_json.get("candidates")
                if not isinstance(candidates, list) or len(candidates) == 0:
                    logger.error(f"Gemini: Invalid response: \'candidates\' missing or empty - {metadata}")
                    return None

                first_candidate = candidates[0]
                if not isinstance(first_candidate, dict):
                    logger.error(f"Gemini: Invalid response: candidate is not a dict - {metadata}")
                    return None

                content = first_candidate.get("content")
                if not isinstance(content, dict):
                    logger.error(f"Gemini: Invalid response: \'content\' missing or not a dict - {metadata}")
                    return None

                parts = content.get("parts")
                if not isinstance(parts, list) or len(parts) == 0:
                    logger.error(f"Gemini: Invalid response: \'parts\' missing or empty - {metadata}")
                    return None

                # Find the part with "text" (may not be first in multi-part responses)
                raw_text = None
                for part in parts:
                    if not isinstance(part, dict):
                        continue
                    if "text" in part and isinstance(part.get("text"), str):
                        raw_text = part.get("text")
                        break

                if raw_text is None:
                    logger.error(f"Gemini: Invalid response: \'text\' missing or not a string in any part - {metadata}")
                    return None

            except (KeyError, TypeError, AttributeError) as exc:
                logger.error(f"Gemini: Failed to parse response structure: {exc}")
                return None

            # Strip markdown code blocks (Gemma and some LLMs wrap JSON in ```json ... ```)
            raw_text = re.sub(r"```(?:json|python)?\s*\n(.*?)\n```", r"\1", raw_text, flags=re.DOTALL)
            raw_text = raw_text.strip()

            json_match = re.search(r"\[\s*{.*}\s*\]", raw_text, re.DOTALL)
            if not json_match:
                logger.info(f"Gemini: No stories returned (no JSON in response) - {metadata}")
                return []

            try:
                stories = json.loads(json_match.group(0))
                if not isinstance(stories, list):
                    logger.error(f"Gemini: JSON parse result is not a list (got {type(stories).__name__})")
                    return None
                logger.info(f"Gemini: {len(stories)} stor{'y' if len(stories) == 1 else 'ies'} generated")
                return stories
            except json.JSONDecodeError as exc:
                logger.error(f"Gemini: Failed to parse JSON from response: {exc}")
                return None

        elif response.status_code == 429:
            try:
                err_msg = response.json().get("error", {}).get(
                    "message", "Rate limit exceeded"
                )
            except Exception:
                err_msg = "Rate limit exceeded"

            # Try to infer which limit: RPD (daily quota) vs RPM (per-minute)
            # RPD = "Resource has been exhausted" or "quota"
            # RPM = "Too many requests" or similar
            is_daily_quota = (
                "exhausted" in err_msg.lower() or
                "quota" in err_msg.lower() or
                "resource" in err_msg.lower()
            )

            if is_daily_quota:
                logger.warning(
                    f"Gemini: 429 daily quota (RPD): {err_msg} — backing off - {metadata}"
                )
                return "quota_exhausted_daily"
            logger.warning(
                f"Gemini: 429 per-minute rate limited (RPM): {err_msg} — backing off - {metadata}"
            )
            return False
        elif response.status_code == 503:
            try:
                err_msg = response.json().get("error", {}).get(
                    "message", "Service temporarily unavailable"
                )
            except Exception:
                err_msg = "Service temporarily unavailable"
            logger.warning(
                f"Gemini: Unavailable (503): {err_msg} — backing off (service recovering) - {metadata}"
            )
            return "service_unavailable"
        else:
            try:
                err_msg = response.json().get("error", {}).get("message", response.text[:120])
            except Exception:
                err_msg = response.text[:120]

            # Detect config errors (invalid model name) vs other errors
            if response.status_code == 404 and "models/" in err_msg and "is not found" in err_msg:
                logger.error(
                    f"Gemini: CONFIG ERROR — invalid gemini_model in TubeNews.json. {err_msg} - {metadata}"
                )
            else:
                logger.error(f"Gemini: HTTP {response.status_code}: {err_msg}")
            return None

    except requests.RequestException as exc:
        logger.error(f"Gemini: Network error: {exc}")
        return None
    except Exception as exc:
        logger.error(f"Gemini: Unexpected error: {exc}", exc_info=True)
        return None


def write_story_files(
    stories: list[GeminiStory],
    meeting_dir: Path,
    video_id: str = "",
    *,
    clear_existing: bool = True,
    start_index: int = 1,
    video_published_at: str = "",
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
        Video published April 5, 2026 at 10:30 AM UTC
        Published April 5, 2026 at 3:15 PM UTC
        **Source:** https://youtu.be/<video_id>?t=120

        Body text …

        ---
        **Segment Start:** 120s

    The ``Video published`` line (when *video_published_at* is supplied) shows
    when YouTube made the source video public.  The ``Published`` line records
    when TubeNews wrote the story — the two will differ by however long
    transcript fetching and AI processing took.
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
            # Optional: when the source video was published on YouTube.
            if video_published_at:
                try:
                    vid_pub_utc = datetime.fromisoformat(
                        video_published_at.replace("Z", "+00:00")
                    ).astimezone(timezone.utc)
                    vid_pub_formatted = (
                        _fmt_no_leading_zeros(vid_pub_utc, "%B %d, %Y")
                        + " at "
                        + _fmt_no_leading_zeros(vid_pub_utc, "%I:%M %p")
                        + " UTC"
                    )
                    fh.write(f"Video published {vid_pub_formatted}\n")
                except Exception:
                    pass
            # Record when TubeNews wrote this story (not the YouTube publish date).
            # Always stored in UTC. Timezone conversion happens at display time in the web app.
            pub_now_utc = datetime.now(timezone.utc)
            tz_abbr = "UTC"
            pub_formatted = (
                _fmt_no_leading_zeros(pub_now_utc, "%B %d, %Y")
                + " at "
                + _fmt_no_leading_zeros(pub_now_utc, "%I:%M %p")
                + f" {tz_abbr}"
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

    meeting_dirs = [d for d in feed_dir.iterdir() if d.is_dir()]

    # Sort by video_date from metadata (newest first)
    def get_sort_key(meeting_dir: Path) -> str:
        metadata_path = meeting_dir / "metadata.json"
        if not metadata_path.exists():
            return ""
        try:
            metadata = json.loads(metadata_path.read_text())
            return metadata.get("video_date", "")
        except Exception:
            return ""

    meeting_dirs = sorted(meeting_dirs, key=get_sort_key, reverse=True)

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
                    datetime.fromtimestamp(_get_timestamp_as_float(metadata.get("processed_at"))).astimezone()
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

    all_stories.sort(key=lambda entry: _get_timestamp_as_float(entry["meta"].get("processed_at")), reverse=True)

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


def build_user_feed_xml(
    user: dict, base_url: str = "", user_id: str = "",
    channel_focus: dict[str, str | list[str]] | None = None,
) -> bytes:
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

    all_stories.sort(key=lambda entry: _get_timestamp_as_float(entry["meta"].get("processed_at")), reverse=True)

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


def rebuild_user_feed(user: dict[str, Any], base_url: str = "", user_id: str = "") -> None:
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


def rebuild_user_feed_page(user: dict[str, Any], base_url: str = "", user_id: str = "") -> None:
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

    all_stories.sort(key=lambda entry: _get_timestamp_as_float(entry["meta"].get("processed_at")), reverse=True)

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
            transcript_link = (
                f" &mdash; <a class='watch' href='{t_url}' target='_blank' rel='noopener'>"
                "&#128221; Read transcript</a>"
            )
        story_blocks.append(
            "<article>\n"
            f"  <h2>{story['title']}</h2>\n"
            f"  <p class='dateline'>{story['dateline']}</p>\n"
            f"  <p class='source'>{entry['channel_name']}"
            + (f" &mdash; <em>{video_title}</em>" if video_title else "")
            + f" &mdash; <a class='watch' href='{yt_url}' target='_blank' rel='noopener'>&#9654; Watch source</a>"
            + transcript_link
            + "</p>\n"
            f"  <div class='body'>{paras}</div>\n"
            "</article>"
        )

    page_title = user.get("feed_name") or f"TubeNews — {name}"
    meta_line = (
        f"{len(all_stories)} stories from {len(subscribed)} channel{'s' if len(subscribed) != 1 else ''}"
    )
    rss_feed_path = f"/feed/{user['feed_token']}.xml"
    rss_link = f'<link rel="alternate" type="application/rss+xml" title="{page_title}" href="{rss_feed_path}">'

    html = """<!DOCTYPE html>
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
    - The directory has ``metadata.json`` but status is "no_transcript_available"
      and less than 3 days have passed (retry in case captions are added later).

    A video with ``metadata.json`` and a final status (e.g., content_written,
    no_captions_final) is considered done and will not be reprocessed.
    """
    if not feed_dir.is_dir():
        return True

    for d in feed_dir.iterdir():
        if d.is_dir() and d.name == video_id:
            metadata_path = d / "metadata.json"
            if not metadata_path.exists():
                return True

            # Check if this is a "no_transcript_available" status from less than 3 days ago
            try:
                metadata = json.loads(metadata_path.read_text())
                status = metadata.get("status", "")
                if status == "no_transcript_available":
                    processed_at_ts = _get_timestamp_as_float(metadata.get("processed_at"))
                    age_seconds = time.time() - processed_at_ts
                    age_days = age_seconds / (24 * 3600)
                    if age_days < 3:
                        # Retry within 3 days; captions may have been added
                        return True
            except (json.JSONDecodeError, KeyError, ValueError):
                # If metadata is invalid, reprocess to recover
                return True

            return False

    return True


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
    video_published_at: str = "",
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
    # pylint: disable=too-many-locals,too-many-positional-arguments,too-many-arguments  # per-video orchestrator
    # Validate video_id to prevent directory traversal attacks
    if not _validate_video_id(video_id):
        return ("skipped", 0)

    if focuses is None:
        focuses = [(feed.get("focus", ""), [])]

    # Locate any pre-existing archive folder for this video ID.
    existing_dir = next(
        (d for d in feed_dir.iterdir() if d.is_dir() and d.name == video_id),
        None,
    )

    channel_name = feed["channel_name"]

    # --- Load or fetch transcript ---
    if existing_dir and (existing_dir / "transcript.txt").exists():
        # Re-use cached transcript; only the AI step needs to re-run (if not disabled).
        logger.info(f"TubeNews: Found cached transcript - {video_id}: {channel_name}: {video_title}")
        transcript_text = (existing_dir / "transcript.txt").read_text(encoding="utf-8")
        meeting_dir = existing_dir
    else:
        # If quota was already known exhausted, don't attempt the API call.
        if transcript_rate_limit_event is not None and transcript_rate_limit_event.is_set():
            return "transcript_quota_exhausted", 0
        counter = f" ({video_num}/{total_videos})" if total_videos else ""
        logger.info(f"TubeNews: Processing new video{counter} - {video_id}: {channel_name}: {video_title}")
        if _is_youtube_short(video_id, feed_name=channel_name):
            logger.info(f"TubeNews: YouTube Short — skipping permanently - {video_id}: {channel_name}: {video_title}")
            short_dir = feed_dir / video_id
            short_dir.mkdir(exist_ok=True)
            (short_dir / "metadata.json").write_text(json.dumps({
                "video_id": video_id,
                "video_title": video_title,
                "video_date": video_date,
                "video_published_at": video_published_at,
                "status": "ignored_short",
                "processed_at": now_utc_iso(),
            }))
            return "skipped", 0
        logger.info(f"Supadata: Fetching transcript - {video_id}: {channel_name}: {video_title}")
        _transcript_failure_reason: list[str] = []
        _livestream_error: list[bool] = []
        transcript_text = fetch_transcript(  # type: ignore[assignment]
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
            # Exception: members-only/restricted and deleted videos are permanent
            # regardless of age — no amount of waiting will produce a transcript.
            _perm_reason = _transcript_failure_reason[0] if _transcript_failure_reason else ""
            if _perm_reason in {"members_only_or_restricted", "video_not_found"}:
                age_hours = float("inf")  # force permanent write-off immediately
            else:
                try:
                    from datetime import datetime as _dt
                    if video_published_at:
                        # Prefer full timestamp for sub-day precision.
                        pub_dt = _dt.fromisoformat(video_published_at.replace("Z", "+00:00"))
                        age_hours = (_dt.now(timezone.utc) - pub_dt).total_seconds() / _SECONDS_PER_HOUR
                    else:
                        pub_dt = _dt.strptime(video_date, "%Y-%m-%d")
                        age_hours = (_dt.now() - pub_dt).total_seconds() / _SECONDS_PER_HOUR
                except Exception as exc:
                    metadata_fmt = f"{video_id}: {channel_name}: {video_title}"
                    logger.debug(f"TubeNews: Failed to parse video publish time: {exc} - {metadata_fmt}")
                    age_hours = float("inf")
            if age_hours < 48:
                metadata_fmt = f"{video_id}: {channel_name}: {video_title}"
                logger.info(
                    f"TubeNews: No transcript yet — video is only {age_hours:.0f}h old, will retry - {metadata_fmt}"
                )
                return "skipped", 0
            # Old enough — permanent.
            if not _validate_iso_date(video_date):
                metadata_fmt = f"{video_id}: {channel_name}: {video_title}"
                logger.error(f"TubeNews: Invalid video_date '{video_date}' — skipping - {metadata_fmt}")
                return "skipped", 0
            meeting_dir = feed_dir / video_id
            meeting_dir.mkdir(exist_ok=True)
            skip_reason = _transcript_failure_reason[0] if _transcript_failure_reason else "no_captions"
            metadata: MetadataDict = {
                "video_id": video_id,
                "video_title": video_title,
                "video_date": video_date,
                "video_published_at": video_published_at,
                "status": "no_transcript_available",
                "skip_reason": skip_reason,
                "processed_at": now_utc_iso(),
            }
            (meeting_dir / "metadata.json").write_text(json.dumps(metadata))
            metadata_fmt = f"{video_id}: {channel_name}: {video_title}"
            logger.info(f"TubeNews: No transcript available — marked permanent, will not retry - {metadata_fmt}")
            return "skipped", 0
        if not transcript_text:
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
                retry_next_try_at = retry_time.isoformat(timespec="seconds").replace('+00:00', 'Z')
                _requeue_video(
                    video_id=video_id,
                    channel_id=channel_id or feed["channel_id"],
                    title=video_title,
                    date=video_date,
                    scheduled_start=scheduled_start,
                    next_try_at=retry_next_try_at,
                    raw_entry_xml=raw_entry_xml,
                )
                metadata_fmt = f"{video_id}: {channel_name}: {video_title}"
                logger.info(f"TubeNews: Livestream detected — re-queued for {retry_next_try_at} - {metadata_fmt}")
                return "skipped", 0
            if transcript_rate_limit_event is not None and transcript_rate_limit_event.is_set():
                return "transcript_quota_exhausted", 0
            metadata_fmt = f"{video_id}: {channel_name}: {video_title}"
            logger.info(f"Supadata: Fetch failed — will retry later - {metadata_fmt}")
            return "skipped", 0

        if not _validate_iso_date(video_date):
            metadata_fmt = f"{video_id}: {channel_name}: {video_title}"
            logger.error(f"TubeNews: Invalid video_date '{video_date}' — skipping - {metadata_fmt}")
            return "skipped", 0
        meeting_dir = feed_dir / video_id
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
    gemini_transient_error = False
    for focus, user_ids in focuses:
        label = f" (focus: {focus!r})" if len(focuses) > 1 else ""
        logger.info(f"Gemini: Generating stories{label} - {video_id}: {channel_name}: {video_title}")
        result = call_gemini_api(
            transcript_text=transcript_text,
            focus=focus,
            video_title=video_title,
            video_date=video_date,
            gemini_api_key=config["gemini_api_key"],
            model_name=config["gemini_model"],
            feed_name=channel_name,
            video_id=video_id,
            call_delay=_safe_float(config.get("gemini_call_delay", 8), 8),
        )
        if result is False:
            # 429 (RPM): rate-limited (per-minute)
            return "ai_rate_limited", 0
        if result == "quota_exhausted_daily":
            # 429 (RPD): daily quota exhausted
            return "quota_exhausted_daily", 0
        if result == "service_unavailable":
            # 503: service temporarily down
            return "service_unavailable", 0
        if result is None:
            gemini_transient_error = True
        elif isinstance(result, list):
            # Process stories returned by Gemini
            for story in result:
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

    # If Gemini hit a transient error, skip metadata write to allow retry.
    if gemini_transient_error:
        logger.info(f"Gemini: Transient error — will retry later - {video_id}: {channel_name}: {video_title}")
        return "skipped", 0

    if all_stories:
        write_story_files(all_stories, meeting_dir, video_id,
                          video_published_at=video_published_at)
        metadata = {
            "video_id": video_id,
            "video_title": video_title,
            "video_date": video_date,
            "video_published_at": video_published_at,
            "status": "processed",
            "processed_at": now_utc_iso(),
            "processed_focuses": sorted(f for f, _ in focuses),
        }
        (meeting_dir / "metadata.json").write_text(json.dumps(metadata))
        n = len(all_stories)
        metadata_fmt = f"{video_id}: {channel_name}: {video_title}"
        story_word = 'y' if n == 1 else 'ies'
        logger.info(f"TubeNews: Done — {n} stor{story_word} written - {metadata_fmt}")
        return "content_written", n

    # Gemini returned no stories for any focus.
    metadata = {
        "video_id": video_id,
        "video_title": video_title,
        "video_date": video_date,
        "video_published_at": video_published_at,
        "status": "no_stories",
        "processed_at": now_utc_iso(),
        "processed_focuses": sorted(f for f, _ in focuses),
    }
    (meeting_dir / "metadata.json").write_text(json.dumps(metadata))
    logger.info(f"TubeNews: Done — no relevant stories found - {video_id}: {channel_name}: {video_title}")
    return "skipped", 0


def process_feed(
    feed: FeedConfig,
    supadata_client: Supadata,
    config: dict,
    ai_rate_limit_event: threading.Event | None = None,
    transcript_rate_limit_event: threading.Event | None = None,
    *,
    forced_videos: list[VideoInfo] | None = None,
) -> tuple[bool, str, int]:
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
        A ``(content_changed, error_type, stories_written)`` tuple.
        *content_changed* is True if any new stories were written (i.e., the
        RSS feed needs to be rebuilt).
        *error_type* is "" (no error), "ai_rate_limited" (429 RPM), "quota_exhausted_daily" (429 RPD),
        or "service_unavailable" (503) if Gemini hit a quota or service issue.
        *stories_written* is the total count of story files created.
    """
    # Validate channel_id to prevent directory traversal attacks
    channel_id = feed.get("channel_id", "")
    if not _validate_channel_id(channel_id):
        logger.error(f"TubeNews: Invalid channel_id '{channel_id}' — skipping feed")
        return (False, "", 0)

    channel_slug = slugify(feed["channel_name"])
    channel_name = feed["channel_name"]
    logger.info(f"TubeNews: Starting feed check - {channel_name}")
    feed_dir = STORAGE_ROOT / channel_slug

    # If the RSS file doesn't exist yet, treat content as changed so we always
    # build an initial feed even if nothing new was processed this run.
    content_changed = not (feed_dir / "rss.xml").exists()
    is_new_feed = not feed_dir.exists()
    error_type = ""  # "", "ai_rate_limited", or "service_unavailable"
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
            logger.info(f"TubeNews: No new videos in push queue - {channel_name}")
            return content_changed, error_type, stories_written
        logger.info(f"TubeNews: Processing {len(videos_to_process)} pushed video(s) - {channel_name}")
        total = len(videos_to_process)
        for video_num, video_info in enumerate(videos_to_process, start=1):
            ai_disabled = bool(error_type) or (
                ai_rate_limit_event is not None and ai_rate_limit_event.is_set()
            )
            # Extract queue entry fields if present (from WebSub daemon)
            queue_entry: dict = video_info.get("_queue_entry") or {}  # type: ignore[assignment]
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
                video_published_at=(
                    video_info.get("published_at", "")  # type: ignore[call-overload]
                    or queue_entry.get("published_at", "")
                ),
            )
            if result == "content_written":
                content_changed = True
                stories_written += n
            elif result == "ai_rate_limited":
                error_type = "ai_rate_limited"
                if ai_rate_limit_event is not None:
                    ai_rate_limit_event.set()
            elif result == "quota_exhausted_daily":
                error_type = "quota_exhausted_daily"
                if ai_rate_limit_event is not None:
                    ai_rate_limit_event.set()
            elif result == "service_unavailable":
                error_type = "service_unavailable"
            elif result == "transcript_quota_exhausted":
                break
        return content_changed, error_type, stories_written

    all_videos = discover_videos(feed["channel_id"], feed_name=channel_name)
    if not all_videos:
        return content_changed, error_type, stories_written

    all_ids = [v["id"] for v in all_videos]
    video_meta = {v["id"]: v for v in all_videos}

    # Videos without metadata.json — new or in recovery (transcript cached, AI failed).
    unprocessed = [v for v in all_videos if _needs_processing(v["id"], feed_dir)]

    if unprocessed:
        logger.info(f"TubeNews: Found {len(unprocessed)} new video(s) - {channel_name}")
    else:
        logger.info(f"TubeNews: No new videos - {channel_name}")

    # Hold same-day videos — YouTube's auto-caption pipeline needs time to
    # finish, and transcript proxies can return garbage for very fresh videos.
    today_str = date.today().isoformat()
    fresh = [
        v for v in unprocessed
        if v["date"] == today_str and not (is_new_feed and all_ids.index(v["id"]) > 0)
    ]
    if fresh:
        noun = "video" if len(fresh) == 1 else "videos"
        logger.info(f"TubeNews: Holding {len(fresh)} {noun} posted today — will process tomorrow - {channel_name}")
        for v in fresh:
            logger.debug(f"TubeNews: Held video {v['id']} (date: {v['date']}) - {channel_name}: {v['title']}")

    if is_new_feed:
        too_old_count = len([v for v in unprocessed if all_ids.index(v["id"]) > 0])
        if too_old_count:
            logger.info(
                f"TubeNews: New feed — marking {too_old_count} existing video(s) as too old to process - {channel_name}"
            )

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
            stub_dir = feed_dir / video_info['id']
            stub_dir.mkdir(exist_ok=True)
            (stub_dir / "metadata.json").write_text(json.dumps({
                "video_id": video_info["id"],
                "video_date": "2000-01-01",
                "status": "ignored_too_old",
                "processed_at": now_utc_iso(),
            }))
            content_changed = True
            continue

        if video_info["date"] == today_str:
            continue  # held until tomorrow's run

        video_num = videos_to_process.index(video_info) + 1
        ai_disabled = bool(error_type) or (
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
            video_published_at=video_info.get("published_at", ""),  # type: ignore[call-overload]
        )

        if result == "content_written":
            content_changed = True
            stories_written += n
        elif result == "ai_rate_limited":
            error_type = "ai_rate_limited"
            if ai_rate_limit_event is not None:
                ai_rate_limit_event.set()
        elif result == "quota_exhausted_daily":
            error_type = "quota_exhausted_daily"
            if ai_rate_limit_event is not None:
                ai_rate_limit_event.set()
        elif result == "service_unavailable":
            error_type = "service_unavailable"
        elif result == "transcript_quota_exhausted":
            # Event already set by process_video; stop wasting time on this feed.
            break

    return content_changed, error_type, stories_written


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
_WSB_RENEWAL_RETRY_COOLDOWN = 3600  # 1 hour — don't retry renewal more frequently
_SECONDS_PER_HOUR = 3600
_SECONDS_PER_DAY = 86400
_PODCAST_TARGET_WORDS = 1300  # ~10 min at 130 WPM with intro/outro overhead
_WEBSUB_POST_TIMEOUT = 10  # seconds for WebSub subscription POST
_GEMINI_TIMEOUT = 150  # seconds for Gemini API calls


def _wsb_topic(channel_id: str) -> str:
    """Return the YouTube Atom feed URL used as the WebSub topic for *channel_id*."""
    return f"https://www.youtube.com/xml/feeds/videos.xml?channel_id={channel_id}"


def _wsb_record_subscription(channel_id: str, callback_url: str) -> None:
    """Write or update the subscription record for *channel_id* in ``state/subscriptions.json``.

    Updates ``last_renew_attempt`` to now to prevent retry spam after successful subscription.
    """
    path = STATE_ROOT / "subscriptions.json"
    try:
        subs: dict = json.loads(path.read_text()) if path.exists() else {}
    except Exception:
        subs = {}
    subs[channel_id] = {
        "subscribed_at": now_utc_iso(),
        "lease_seconds": _WSB_LEASE,
        "callback_url": callback_url,
        "last_renew_attempt": now_utc_iso(),
    }
    _atomic_write(path, json.dumps(subs, indent=2))


def _wsb_remove_subscription(channel_id: str) -> None:
    """Remove the subscription record for *channel_id* from ``state/subscriptions.json``."""
    path = STATE_ROOT / "subscriptions.json"
    if not path.exists():
        return
    try:
        subs: dict = json.loads(path.read_text())
    except Exception as exc:
        logger.error(f"Failed to read subscriptions file {path}: {exc}")
        return
    subs.pop(channel_id, None)
    _atomic_write(path, json.dumps(subs, indent=2))


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
        }, timeout=_WEBSUB_POST_TIMEOUT)
        ok = r.status_code == 202
        if ok:
            _wsb_record_subscription(channel_id, cb)
            logger.info(f"WebSub: subscription confirmed for {channel_id}")
        else:
            logger.warning(
                f"WebSub: subscription request returned HTTP {r.status_code} for {channel_id} "
                "(transient; will retry later)"
            )
        return ok
    except Exception as exc:
        logger.warning(f"WebSub: subscription request failed for {channel_id}: {exc} (transient; will retry)")
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
        }, timeout=_WEBSUB_POST_TIMEOUT)
        ok = r.status_code == 202
        if ok:
            _wsb_remove_subscription(channel_id)
            logger.info(f"WebSub: unsubscribed channel {channel_id}")
        else:
            logger.warning(f"WebSub: unsubscribe returned HTTP {r.status_code} for {channel_id}")
        return ok
    except Exception as exc:
        logger.warning(f"WebSub: unsubscribe failed for {channel_id}: {exc}")
        return False


# ---------------------------------------------------------------------------
# Push queue helpers — used by --daemon mode
# ---------------------------------------------------------------------------


def _read_push_queue() -> list[dict]:
    """Return queue entries that are due for processing.

    An entry is ripe when its ``next_try_at`` timestamp has passed.  Legacy
    entries that pre-date the ``next_try_at`` field (absent or ``None``) are
    treated as immediately ripe so they are not silently abandoned.

    Returns:
        List of ripe queue dicts, each with at least ``video_id`` and
        ``channel_id`` keys.  Returns ``[]`` when the queue file is absent
        or cannot be parsed.
    """
    path = STATE_ROOT / "queue" / "push_queue.json"
    if not path.exists():
        return []
    try:
        items: list[dict] = json.loads(path.read_text())
    except Exception as exc:
        logger.error(f"Queue read failed ({path}): {exc}")
        return []
    now = datetime.now(timezone.utc)
    result = []
    for i in items:
        nta = i.get("next_try_at")
        if nta is None:
            # No next_try_at: legacy entry or orphan — process immediately.
            result.append(i)
        else:
            try:
                if datetime.fromisoformat(nta.replace("Z", "+00:00")) <= now:
                    result.append(i)
            except (ValueError, TypeError):
                result.append(i)  # malformed timestamp — treat as ripe
    return result


_QUEUE_MAX_RETRIES = 10

# Transcript retry schedule: seconds from queued_at for each successive attempt.
# Attempt 0 → T+5 min (first check, shortly after notification).
# Attempts 1–16 → T+1h through T+24h (spaced over 24 hours).
# After _TRANSCRIPT_MAX_ATTEMPTS failures the video is marked permanently no-transcript.
# This allows captions to appear later and services (like Supadata) to recover.
_TRANSCRIPT_RETRY_OFFSETS: tuple[int, ...] = (
    5 * 60,       # attempt 0:  T+5 min  (first check)
    1 * 3600,     # attempt 1:  T+1 hr
    2 * 3600,     # attempt 2:  T+2 hr
    3 * 3600,     # attempt 3:  T+3 hr
    4 * 3600,     # attempt 4:  T+4 hr
    5 * 3600,     # attempt 5:  T+5 hr
    6 * 3600,     # attempt 6:  T+6 hr
    7 * 3600,     # attempt 7:  T+7 hr
    8 * 3600,     # attempt 8:  T+8 hr
    9 * 3600,     # attempt 9:  T+9 hr
    10 * 3600,    # attempt 10: T+10 hr
    12 * 3600,    # attempt 11: T+12 hr
    15 * 3600,    # attempt 12: T+15 hr
    18 * 3600,    # attempt 13: T+18 hr
    21 * 3600,    # attempt 14: T+21 hr
    24 * 3600,    # attempt 15: T+24 hr
    30 * 3600,    # attempt 16: T+30 hr  (final; failure → permanent)
)
_TRANSCRIPT_MAX_ATTEMPTS: int = len(_TRANSCRIPT_RETRY_OFFSETS)  # 13


def _next_transcript_try(queued_at_iso: str, attempt: int) -> str:
    """Compute the ISO 8601 timestamp for the next transcript fetch attempt.

    Args:
        queued_at_iso: Original notification timestamp (ISO 8601, Z-suffixed).
        attempt: The attempt number being scheduled (0 = first try at T+5min).

    Returns:
        ISO 8601 UTC string for when to next attempt the transcript fetch.
    """
    idx = min(attempt, len(_TRANSCRIPT_RETRY_OFFSETS) - 1)
    try:
        base = datetime.fromisoformat(queued_at_iso.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        base = datetime.now(timezone.utc)
    result = base + timedelta(seconds=_TRANSCRIPT_RETRY_OFFSETS[idx])
    return result.isoformat(timespec="seconds").replace("+00:00", "Z")


def _update_queue_entries(updated_entries: list[dict]) -> None:
    """Persist updated queue entries back to ``push_queue.json``.

    Reads ``push_queue.json``, replaces matching entries with the updated
    versions (which may carry incremented ``retry_count``, updated
    ``next_try_at``, or incremented ``transcript_attempts``), and writes
    back atomically.  No-op when the queue file is absent.
    """
    path = STATE_ROOT / "queue" / "push_queue.json"
    if not path.exists():
        return
    try:
        items: list[dict] = json.loads(path.read_text())
    except Exception as exc:
        logger.error(f"Queue read failed ({path}): {exc}")
        return
    try:
        by_vid = {e["video_id"]: e for e in updated_entries}
        merged = [by_vid.get(i.get("video_id"), i) for i in items]
        _atomic_write(path, json.dumps(merged, indent=2))
    except Exception as exc:
        logger.error(f"Queue entry update failed ({path}): {exc}")


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
    except Exception as exc:
        logger.error(f"Queue read failed ({path}): {exc}")
        return
    try:
        remaining = [i for i in items if i.get("video_id") not in processed_ids]
        _atomic_write(path, json.dumps(remaining, indent=2))
    except Exception as exc:
        logger.error(f"Queue removal update failed ({path}): {exc}")


_MEDIA_NS = {"media": "http://search.yahoo.com/mrss/"}


def _title_from_entry_el(entry_el: "ET.Element") -> str:
    """Extract the best available title from an Atom ``<entry>`` XML element.

    YouTube WebSub notifications include the video title in (at least) two
    places:

    1. ``<title>`` in the Atom namespace — present in standard notifications.
    2. ``<media:group><media:title>`` in the MediaRSS namespace — present in
       some notifications, and always matches the ``<title>`` when both exist.

    We try both so that a notification with an empty ``<title>`` but a
    populated ``<media:title>`` still yields the correct title.

    Args:
        entry_el: A parsed ``xml.etree.ElementTree.Element`` for the
                  ``<entry>`` node of a YouTube Atom notification.

    Returns:
        The video title string, or ``""`` if neither field is populated.
    """
    # 1. Standard Atom <title>
    title_el = entry_el.find("atom:title", _YT_NS)
    if title_el is not None and title_el.text:
        return title_el.text.strip()

    # 2. MediaRSS <media:group><media:title>
    group_el = entry_el.find("media:group", _MEDIA_NS)
    if group_el is not None:
        mt = group_el.find("media:title", _MEDIA_NS)
        if mt is not None and mt.text:
            return mt.text.strip()

    return ""


def _title_from_raw_xml(raw_xml: str) -> str:
    """Parse a stored WebSub entry XML fragment and extract the video title.

    Accepts the ``raw_entry_xml`` string stored in push-queue entries and
    delegates to :func:`_title_from_entry_el` for the actual extraction.
    Used at processing time so that a queue entry whose ``title`` field is
    empty — but whose ``raw_entry_xml`` was later updated by a second
    notification that carried the title — can still be processed with the
    correct title without any external network calls.

    Fails open: returns ``""`` on any parse error.

    Args:
        raw_xml: Raw XML string of a single ``<entry>`` element from a
                 YouTube WebSub Atom notification.

    Returns:
        The video title string, or ``""`` if it cannot be extracted.
    """
    if not raw_xml:
        return ""
    try:
        entry_el = ET.fromstring(raw_xml)
        return _title_from_entry_el(entry_el)
    except Exception as exc:
        logger.debug(f"Raw entry XML title parse failed: {exc}")
        return ""


def _merge_queue_entry(existing: dict, new_entry: dict) -> dict:
    """Merge a newer WebSub notification into an existing push-queue entry.

    When YouTube sends a second notification for the same video (e.g. because
    the channel owner edited the title or description), we want to:

    * Pull in fresh metadata (``raw_entry_xml``, ``scheduled_start``, ``date``)
      from the new notification.
    * Update ``title`` only when the new notification carries a non-empty one —
      a blank title in a re-push must not overwrite a title we already have.
    * **Preserve** all retry state (``queued_at``, ``next_try_at``,
      ``transcript_attempts``, ``retry_count``) so the retry schedule is not
      inadvertently reset by a metadata-only re-push.

    Args:
        existing:  The queue entry currently stored in the push queue.
        new_entry: The fresh entry parsed from the incoming WebSub notification.

    Returns:
        Merged dict ready to be written back to the push queue.
    """
    return {
        **new_entry,
        # Keep the original enqueueing timestamp.
        "queued_at":           existing.get("queued_at", new_entry["queued_at"]),
        # Preserve the existing retry schedule; don't reset the timer.
        "next_try_at":         existing.get("next_try_at", new_entry["next_try_at"]),
        "transcript_attempts": existing.get("transcript_attempts", 0),
        "retry_count":         existing.get("retry_count", 0),
        # Keep the known title when the new notification omits it.
        "title":        new_entry.get("title") or existing.get("title", ""),
        # Keep the known publish timestamp when the new notification omits it.
        "published_at": new_entry.get("published_at") or existing.get("published_at", ""),
    }


def _requeue_video(
    video_id: str,
    channel_id: str,
    title: str,
    date: str,
    scheduled_start: str | None,
    next_try_at: str,
    raw_entry_xml: str = "",
) -> None:
    """Re-queue a video for later processing (livestream still broadcasting).

    Sets ``next_try_at`` to *next_try_at* so the processor waits until after
    the stream ends before fetching the transcript again.  Resets
    ``transcript_attempts`` to 0 — the stream not being over is not a
    transcript failure, and we want a fresh retry window once it ends.
    Existing ``retry_count`` and ``queued_at`` are preserved.

    Args:
        video_id: YouTube video ID.
        channel_id: YouTube channel ID.
        title: Video title.
        date: Video publish date (ISO 8601).
        scheduled_start: Scheduled stream start time (ISO 8601), or ``None``.
        next_try_at: ISO 8601 UTC timestamp for when to next attempt processing.
        raw_entry_xml: Raw Atom entry XML from WebSub notification.
    """
    queue_dir = STATE_ROOT / "queue"
    queue_dir.mkdir(parents=True, exist_ok=True)
    queue_path = queue_dir / "push_queue.json"

    with _queue_lock:
        try:
            items: list[dict] = json.loads(queue_path.read_text()) if queue_path.exists() else []
        except Exception as exc:
            logger.warning(f"Queue read failed ({queue_path}): {exc}")
            items = []

        try:
            # Preserve existing entry's queued_at and retry_count; reset transcript_attempts.
            by_vid = {i["video_id"]: i for i in items}
            existing = by_vid.get(video_id, {})
            existing_queued_at = existing.get("queued_at", now_utc_iso())
            existing_retry_count = existing.get("retry_count", 0)

            entry = {
                "video_id": video_id,
                "channel_id": channel_id,
                "title": title,
                "date": date,
                "scheduled_start": scheduled_start,
                "raw_entry_xml": raw_entry_xml,
                "queued_at": existing_queued_at,
                "next_try_at": next_try_at,
                "transcript_attempts": 0,  # fresh retry window after stream ends
                "retry_count": existing_retry_count,
            }
            by_vid[video_id] = entry

            updated = list(by_vid.values())
            tmp = queue_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(updated, indent=2))
            tmp.replace(queue_path)
        except Exception as exc:
            logger.error(f"Requeue failed for {video_id} ({queue_path}): {exc}")


def _write_no_transcript_metadata(
    video_id: str,
    feed_dir: Path,
    video_date: str,
    video_title: str,
    skip_reason: str = "no_captions",
    channel_name: str = "",
    video_published_at: str = "",
) -> None:
    """Write a permanent ``no_transcript_available`` metadata.json for a video.

    Creates the meeting directory if needed.  Called from both
    :func:`process_video` (for the 48-hour age path) and the WebSub
    (when all transcript retry attempts are exhausted).

    Args:
        video_id: YouTube video ID.
        feed_dir: Channel content directory (``STORAGE_ROOT / slugify(channel_name)``).
        video_date: Video date in ``YYYY-MM-DD`` format (stored in metadata).
        video_title: Video title (stored in metadata for human reference).
        skip_reason: Reason code — ``"no_captions"``, ``"members_only_or_restricted"``,
            or ``"video_not_found"``.
        channel_name: Channel name for log prefix.
        video_published_at: Full ISO 8601 publish timestamp; ``""`` when unknown.
    """
    meeting_dir = feed_dir / video_id
    meeting_dir.mkdir(parents=True, exist_ok=True)
    metadata: MetadataDict = {
        "video_id": video_id,
        "video_title": video_title,
        "video_date": video_date,
        "video_published_at": video_published_at,
        "status": "no_transcript_available",
        "skip_reason": skip_reason,
        "processed_at": now_utc_iso(),
    }
    (meeting_dir / "metadata.json").write_text(json.dumps(metadata))
    # Standardized log format: "channel: video_title (video_id):"
    prefix_parts = [p for p in [channel_name, video_title] if p]
    if prefix_parts:
        prefix = ": ".join(prefix_parts) + f" ({video_id}):"
    else:
        prefix = f"[{video_id}]:"
    logger.info(
        f"{prefix} No transcript available — "
        f"marked permanent (reason: {skip_reason})"
    )


def _wsb_try_fetch_transcript(
    entry: dict,
    feed_cfg: FeedConfig,
    supadata_client: object,
    transcript_rate_limit_event: threading.Event | None,
) -> str:
    """Try to fetch and cache the transcript for a single queue entry.

    Checks whether ``transcript.txt`` already exists in the video's meeting
    directory.  If not, calls :func:`fetch_transcript` and writes the result
    to disk on success.

    This function is called by the WebSub's transcript-phase loop
    before the Gemini phase.  It never runs Gemini — only Supadata.

    Args:
        entry: Queue entry dict (must have ``video_id``, ``date``, ``title``).
        feed_cfg: Channel config dict (must have ``channel_name``).
        supadata_client: Authenticated Supadata client instance.
        transcript_rate_limit_event: Threading event; set when Supadata quota
            is exhausted so the processor can halt further transcript fetches.

    Returns:
        One of:
        ``"cached"``         — transcript.txt already on disk (from a prior run).
        ``"success"``        — transcript fetched and written to transcript.txt.
        ``"permanent"``      — Supadata confirmed the video is members-only/restricted or
                               not found; metadata written immediately, video resolved.
        ``"transient"``      — temporary failure (no_captions retries up to
                               _TRANSCRIPT_MAX_ATTEMPTS before being written off).
        ``"livestream"``     — video is a live stream still broadcasting.
        ``"quota_exhausted"``— Supadata credit quota is exhausted this session.
    """
    video_id = entry.get("video_id", "")
    feed_dir = STORAGE_ROOT / slugify(feed_cfg["channel_name"])
    meeting_dir = feed_dir / video_id

    if (meeting_dir / "transcript.txt").exists():
        return "cached"

    meeting_dir.mkdir(parents=True, exist_ok=True)

    failure_reason: list[str] = []
    livestream_error: list[bool] = []
    result = fetch_transcript(
        video_id,
        supadata_client,
        feed_name=feed_cfg.get("channel_name", ""),
        video_title=entry.get("title", ""),
        transcript_rate_limit_event=transcript_rate_limit_event,
        failure_reason=failure_reason,
        livestream_error=livestream_error,
    )

    if result is None:
        if transcript_rate_limit_event is not None and transcript_rate_limit_event.is_set():
            return "quota_exhausted"
        if livestream_error and livestream_error[0]:
            return "livestream"
        return "transient"

    if result is False:
        reason = failure_reason[0] if failure_reason else ""
        if reason in {"members_only_or_restricted", "video_not_found"}:
            # Genuinely permanent: paywall or deleted video will never have a transcript.
            video_date = entry.get("date", "")[:10]
            _write_no_transcript_metadata(
                video_id, feed_dir, video_date, entry.get("title", ""),
                skip_reason=reason,
                channel_name=feed_cfg.get("channel_name", ""),
                video_published_at=entry.get("published_at", ""),
            )
            return "permanent"
        # no_captions: captions may appear later — retry per the normal schedule.
        return "transient"

    # Transcript text successfully returned — cache it for the Gemini phase.
    (meeting_dir / "transcript.txt").write_text(result, encoding="utf-8")
    channel_name = feed_cfg.get("channel_name", "")
    video_title = entry.get("title", "")
    prefix_parts = [p for p in [channel_name, video_title] if p]
    if prefix_parts:
        prefix = ": ".join(prefix_parts) + f" ({video_id})"
    else:
        prefix = f"[{video_id}]"
    logger.debug(f"{prefix}: Transcript cached at {meeting_dir / 'transcript.txt'}")
    return "success"


def _recover_orphaned_videos() -> int:
    """Scan the content archive for meeting dirs that have no ``metadata.json``.

    Such directories represent videos that were downloaded (or partially
    processed) but never completed — e.g. the daemon was interrupted mid-run,
    or the operator deleted a ``metadata.json`` to force a re-run.

    Each orphaned video is added to the push queue with ``queued_at = None``
    and ``next_try_at = None`` so it is immediately ripe on the next processor
    cycle.  Videos already in the queue are left untouched.

    Returns the number of newly queued videos.
    """
    queue_dir = STATE_ROOT / "queue"
    queue_dir.mkdir(parents=True, exist_ok=True)
    queue_path = queue_dir / "push_queue.json"

    # Load existing queue so we don't duplicate entries
    try:
        existing: list[dict] = json.loads(queue_path.read_text()) if queue_path.exists() else []
    except Exception as exc:
        logger.warning(f"Queue read failed during orphan recovery ({queue_path}): {exc}")
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
        except Exception as exc:
            logger.warning(f"Skipping corrupted channel.json at {channel_json}: {exc}")
            continue
        channel_id = cinfo.get("channel_id", "")
        if not channel_id:
            continue

        for meeting_dir in sorted(channel_dir.iterdir()):
            if not meeting_dir.is_dir():
                continue
            if (meeting_dir / "metadata.json").exists():
                continue
            # Directory name is now just the video_id
            video_id = meeting_dir.name
            if not video_id or video_id in already_queued:
                continue
            # Try to get date from metadata if present, else empty string
            vid_date = ""
            meta_path = meeting_dir / "metadata.json"
            if meta_path.exists():
                try:
                    vid_date = json.loads(meta_path.read_text()).get("video_date", "")
                except Exception as exc:
                    logger.debug(f"Could not read video_date from {meta_path}: {exc}")
            new_entries.append({
                "video_id":          video_id,
                "channel_id":        channel_id,
                "title":             "",
                "date":              vid_date,
                "queued_at":         None,
                "next_try_at":       None,  # immediately ripe
                "transcript_attempts": 0,
            })
            already_queued.add(video_id)

    if new_entries:
        try:
            by_vid = {e["video_id"]: e for e in existing}
            for ne in new_entries:
                by_vid[ne["video_id"]] = ne
            tmp = queue_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(list(by_vid.values()), indent=2))
            tmp.replace(queue_path)
            ids = ", ".join(e["video_id"] for e in new_entries)
            logger.info(f"Orphan recovery: queued {len(new_entries)} video(s): {ids}")
        except Exception as exc:
            logger.error(f"Orphan recovery queue update failed ({queue_path}): {exc}")

    return len(new_entries)


# ---------------------------------------------------------------------------
# --daemon mode — WebSub receiver + processor threads
# ---------------------------------------------------------------------------

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
    port = _safe_int(config.get("websub_daemon_port", 8675), 8675)
    secret = config.get("websub_secret", "").encode()
    channels = _read_channels()
    known_topics = {_wsb_topic(ch["channel_id"]): ch["channel_id"] for ch in channels}
    channel_by_id = {ch["channel_id"]: ch["channel_name"] for ch in channels}

    queue_dir = STATE_ROOT / "queue"
    queue_dir.mkdir(parents=True, exist_ok=True)
    queue_path = queue_dir / "push_queue.json"

    class _Handler(_http_server.BaseHTTPRequestHandler):
        def log_message(self, format, *args):  # silence access log  # pylint: disable=redefined-builtin
            logger.debug("WebSub receiver: " + format % args)

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
            channel_id = known_topics[topic]
            channel_name = channel_by_id.get(channel_id, "?")
            logger.info(f"WebSub: verified subscription for {channel_name} ({channel_id})")

        def do_POST(self):
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length)

            sig_header = self.headers.get("X-Hub-Signature", "")
            if secret and sig_header.startswith("sha1="):
                expected = _hmac.new(secret, body, hashlib.sha1).hexdigest()
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
                pub_el   = entry.find("atom:published", _YT_NS)
                sched_el = entry.find("yt:scheduledStartTime", _YT_NS)
                if vid_el is not None and ch_el is not None:
                    pub_raw = (pub_el.text or "").strip() if pub_el is not None else ""
                    # Normalize date to YYYY-MM-DD format (truncate full ISO timestamp)
                    pub_date = pub_raw.split("T")[0] if pub_raw else ""
                    sched_start = (sched_el.text or "").strip() if sched_el is not None else None
                    # Preserve complete entry for future metadata extraction
                    raw_entry = ET.tostring(entry, encoding='unicode')
                    new_entries.append({
                        "video_id":   vid_el.text.strip(),
                        "channel_id": ch_el.text.strip(),
                        # Try atom:title first; fall back to media:title inside media:group.
                        "title":      _title_from_entry_el(entry),
                        "date":       pub_date,
                        "published_at": pub_raw,  # full ISO 8601; "" when absent
                        "scheduled_start": sched_start,
                        "raw_entry_xml": raw_entry,
                        "queued_at":  now,
                        "next_try_at": _next_transcript_try(now, 0),  # T+5 min
                        "transcript_attempts": 0,
                    })

            if new_entries:
                try:
                    with _queue_lock:
                        try:
                            existing: list[dict] = (
                                json.loads(queue_path.read_text()) if queue_path.exists() else []
                            )
                        except Exception as exc:
                            logger.warning(f"WebSub: queue read failed ({queue_path}): {exc}")
                            existing = []
                        try:
                            by_vid = {e["video_id"]: e for e in existing}
                            queued_entries: list[dict] = []
                            for ne in new_entries:
                                ne_vid = ne["video_id"]
                                # Skip videos already fully processed (metadata.json exists).
                                # channel_by_id is a startup snapshot; unknown channels pass through.
                                ne_ch = channel_by_id.get(ne.get("channel_id", ""))
                                if ne_ch:
                                    ne_dir = STORAGE_ROOT / slugify(ne_ch)
                                    if not _needs_processing(ne_vid, ne_dir):
                                        logger.debug(
                                            f"WebSub: {ne_ch}: {ne.get('title', ne_vid)!r}"
                                            f" ({ne_vid}) already processed"
                                            " — ignoring duplicate push"
                                        )
                                        continue
                                if ne_vid in by_vid:
                                    ne = _merge_queue_entry(by_vid[ne_vid], ne)
                                by_vid[ne_vid] = ne
                                queued_entries.append(ne)
                            if queued_entries:
                                updated = list(by_vid.values())
                                tmp = queue_path.with_suffix(".tmp")
                                tmp.write_text(json.dumps(updated, indent=2))
                                tmp.replace(queue_path)
                                # Log with channel name and video titles for clarity
                                video_strs = [
                                    f"{e.get('title', '?')} ({e['video_id']})"
                                    for e in queued_entries
                                ]
                                ch_id = queued_entries[0].get("channel_id", "?")
                                ch_name = channel_by_id.get(ch_id, "?")
                                logger.info(
                                    f"WebSub: {ch_name}: queued {len(queued_entries)} video(s):"
                                    f" {', '.join(video_strs)}"
                                )
                        except Exception as exc:
                            logger.error(f"WebSub: queue write failed ({queue_path}): {exc}")
                except Exception as exc:
                    logger.error(f"WebSub: queue lock acquisition failed: {exc}")

            self.send_response(204)
            self.end_headers()

    server = _http_server.HTTPServer(("0.0.0.0", port), _Handler)
    logger.info(f"WebSub: receiver listening on 0.0.0.0:{port}")
    server.serve_forever()


def _wsb_processor_thread(config: dict) -> None:
    """Thread 2: periodically checks the push queue and processes ripe entries.

    On each wake-up the processor runs two sequential phases:

    **Phase 1 — Transcript fetching (uncapped):**
    For every ripe entry that does not yet have a cached ``transcript.txt``,
    calls :func:`_wsb_try_fetch_transcript`.  On transient failure the entry's
    ``next_try_at`` is advanced per :data:`_TRANSCRIPT_RETRY_OFFSETS` (T+5 min,
    T+1 h, T+2 h, … T+12 h).  After :data:`_TRANSCRIPT_MAX_ATTEMPTS` failures
    the video is permanently marked ``no_transcript_available``.  Entries with
    existing ``transcript.txt`` pass straight to Phase 2.

    **Phase 2 — Gemini processing (capped by** ``websub_max_videos_per_cycle``):**
    Calls :func:`process_feed` for each channel that has transcript-ready videos,
    up to *max_per_cycle* videos total.  A Gemini rate-limit backoff only affects
    this phase; transcript fetching (Phase 1) continues unimpeded.

    Other tasks each cycle:
    1. **Config reload:** checks TubeNews.json for changes and applies them.
    2. **Renewal check:** re-subscribes channels whose WebSub lease expires
       within the next 24 hours.
    3. **Orphan recovery (once per day):** scans the content archive for meeting
       directories without ``metadata.json`` and queues them.

    Sleep interval is ``websub_check_interval_minutes`` (default 1 minute).
    Runs until the process exits (daemon thread).
    """
    # pylint: disable=too-many-locals  # orchestrator thread; locals are named state, not complexity
    # Backoff times for Gemini errors:
    # - 429 RPM (rate-limited per-minute): shorter backoff; recoverable quickly
    # - 429 RPD (daily quota exhausted): 12 hours; quota resets sometime during the day
    # - 503 (service down): longer backoff; service needs recovery time
    # Videos stay in the queue; only the AI step is skipped during backoff.
    _AI_BACKOFF_QUOTA_RPM = 120  # 2 minutes for 429 RPM
    _AI_BACKOFF_QUOTA_RPD = 43200  # 12 hours for 429 RPD (safer than 24h)
    _AI_BACKOFF_SERVICE = 300  # 5 minutes for 503
    _ai_backoff_until: float = 0.0
    _deprecated_min_age_warned = False

    # Run orphan recovery immediately on startup, then once per 24 h.
    try:
        _recover_orphaned_videos()
    except Exception as exc:
        logger.warning(f"WebSub: Orphan recovery failed - {exc}")
    _last_orphan_recovery: float = time.time()
    _last_digest_check: float = 0.0
    _last_podcast_check: float = 0.0
    _last_heartbeat: float = 0.0
    _heartbeat_interval: float = 300  # Log heartbeat every 5 minutes

    while True:
        # -- Config reload ----------------------------------------------------
        _reload_config_from_disk()

        # Read current values from reloadable config
        with _config_lock:
            interval = _safe_float(_daemon_config.get("websub_check_interval_minutes", 1), 1) * 60
            supadata_key = _daemon_config.get("supadata_api_key")
            # Deprecation notice: websub_min_age_minutes is superseded by per-entry
            # next_try_at scheduling and is no longer read by _read_push_queue.
            if "websub_min_age_minutes" in _daemon_config and not _deprecated_min_age_warned:
                logger.warning(
                    "websub_min_age_minutes is deprecated and has no effect. "
                    "Per-entry next_try_at scheduling is used instead. "
                    "You may remove it from TubeNews.json."
                )
                _deprecated_min_age_warned = True
            # Make a shallow copy of _daemon_config for use outside the lock
            current_config = _daemon_config.copy()
        supadata_client = Supadata(api_key=supadata_key)

        # -- Renewal check (respects cooldown to prevent spam) ------------------
        subs_path = STATE_ROOT / "subscriptions.json"
        if subs_path.exists():
            try:
                subs: dict = json.loads(subs_path.read_text())
            except Exception as exc:
                logger.warning(f"WebSub: Subscriptions.json corrupted, resetting - {exc}")
                subs = {}
            channels = _read_channels()
            channel_by_id = {ch["channel_id"]: ch["channel_name"] for ch in channels}
            renew_before = time.time() + _SECONDS_PER_DAY  # within next 24 h
            now_ts = time.time()
            for cid, info in subs.items():
                subscribed_at = _get_timestamp_as_float(info.get("subscribed_at", 0))
                expires = subscribed_at + info.get("lease_seconds", _WSB_LEASE)
                if expires <= renew_before:
                    # Check cooldown to avoid renewal spam; respect last_renew_attempt timestamp
                    last_attempt = _get_timestamp_as_float(info.get("last_renew_attempt", 0))
                    time_since_attempt = now_ts - last_attempt
                    should_retry = time_since_attempt >= _WSB_RENEWAL_RETRY_COOLDOWN

                    ch_name = channel_by_id.get(cid, "?")
                    if should_retry:
                        expires_in = int(expires - now_ts)
                        logger.debug(
                            f"WebSub: Attempting renewal ({ch_name} / {cid}); expires in {expires_in}s"
                        )
                        _wsb_subscribe(cid, current_config)
                    else:
                        remaining_cooldown = int(_WSB_RENEWAL_RETRY_COOLDOWN - time_since_attempt)
                        logger.debug(
                            f"WebSub: Deferring renewal ({ch_name} / {cid}); will retry in {remaining_cooldown}s"
                        )

        # -- Orphan recovery (once per 24 h) ----------------------------------
        if time.time() - _last_orphan_recovery >= _SECONDS_PER_DAY:
            try:
                _recover_orphaned_videos()
            except Exception as exc:
                logger.warning(f"WebSub: Orphan recovery failed - {exc}")
            _last_orphan_recovery = time.time()

        # -- Daily email digest -----------------------------------------------
        digest_send_hour = int(current_config.get("email_digest_send_hour", 7))
        if (datetime.utcnow().hour == digest_send_hour
                and time.time() - _last_digest_check >= _SECONDS_PER_DAY):
            try:
                _send_daily_digests(current_config)
            except Exception as exc:
                logger.warning(f"Daily digest: unexpected error: {exc}")
            _last_digest_check = time.time()

        # -- Daily podcast generation -----------------------------------------
        podcast_hour = int(current_config.get("podcast_generation_hour", 6))
        if (datetime.utcnow().hour == podcast_hour
                and time.time() - _last_podcast_check >= _SECONDS_PER_DAY):
            try:
                _generate_daily_podcasts(current_config)
            except Exception as exc:
                logger.warning("Daily podcast: unexpected error: %s", exc, exc_info=True)
            _last_podcast_check = time.time()

        # -- Queue processing -------------------------------------------------
        ripe = _read_push_queue()
        if not ripe:
            time.sleep(interval)
            continue

        if not _acquire_lock():
            logger.debug("WebSub: Lock held by another process, skipping cycle")
            time.sleep(interval)
            continue

        try:
            all_channels = _read_channels()
            # Only process enabled channels; disabled channels' queued videos are dropped
            channels = [ch for ch in all_channels if not ch.get("disabled", False)]
            channel_map = {ch["channel_id"]: ch for ch in channels}

            # Drain the queue aggressively while service is healthy.
            # Only break when we hit 503 (service down) or similar.
            # websub_max_videos_per_cycle config is now ignored; we process all ready videos.
            max_per_cycle = 10000  # Effectively unlimited; 503 is the natural brake

            ai_in_backoff = time.time() < _ai_backoff_until
            if ai_in_backoff:
                remaining = int(_ai_backoff_until - time.time())
                logger.info(
                    f"WebSub: Gemini backoff active — skipping AI this cycle ({remaining}s remaining)"
                )
            ai_event = threading.Event()
            if ai_in_backoff:
                ai_event.set()  # pre-set so process_feed skips AI immediately
            transcript_event = threading.Event()

            resolved_ids: set[str] = set()
            retry_updates: list[dict] = []

            # ----------------------------------------------------------------
            # Phase 1 — Transcript fetching (no Gemini cap)
            # ----------------------------------------------------------------
            transcript_ready: list[dict] = []

            for entry in ripe:
                vid = entry.get("video_id", "")
                cid = entry.get("channel_id", "")
                date_str = entry.get("date", "")

                # Drop future-dated entries without retrying — the date check
                # prevents processing videos before they are published.
                if date_str:
                    try:
                        pub_time = datetime.fromisoformat(date_str.replace("Z", "+00:00")).timestamp()
                        if pub_time > time.time():
                            # Defer this entry until after its publish date
                            retry_dt = datetime.fromtimestamp(pub_time, tz=timezone.utc) + timedelta(minutes=5)
                            next_try_at = retry_dt.isoformat(timespec="seconds").replace("+00:00", "Z")
                            retry_updates.append({
                                **entry,
                                "next_try_at": next_try_at,
                            })
                            resolved_ids.add(vid)
                            continue
                    except (ValueError, TypeError):
                        pass  # Malformed date — proceed

                feed_cfg = channel_map.get(cid)
                if not feed_cfg:
                    # Channel removed from config — discard the entry.
                    resolved_ids.add(vid)
                    continue

                feed_dir = STORAGE_ROOT / slugify(feed_cfg["channel_name"])

                # Already fully processed (metadata.json exists) — clean up.
                if not _needs_processing(vid, feed_dir):
                    resolved_ids.add(vid)
                    continue

                # Try to obtain the transcript (returns immediately if cached).
                fetch_result = _wsb_try_fetch_transcript(
                    entry, feed_cfg, supadata_client, transcript_event
                )

                if fetch_result in ("success", "cached"):
                    transcript_ready.append(entry)

                elif fetch_result == "quota_exhausted":
                    logger.warning("WebSub: Supadata quota exhausted — halting transcript fetches")
                    break  # Leave remaining entries in queue unchanged

                elif fetch_result == "livestream":
                    # Re-queue with next_try_at deferred past when the stream ends.
                    # Use scheduled_start if present; fall back to now + 1 hour.
                    scheduled_start = entry.get("scheduled_start")
                    if scheduled_start:
                        try:
                            stream_end = datetime.fromisoformat(
                                scheduled_start.replace("Z", "+00:00")
                            )
                            retry_dt = stream_end + timedelta(hours=1)
                        except (ValueError, TypeError):
                            retry_dt = datetime.now(timezone.utc) + timedelta(hours=1)
                    else:
                        retry_dt = datetime.now(timezone.utc) + timedelta(hours=1)
                    next_try_at = retry_dt.isoformat(timespec="seconds").replace("+00:00", "Z")
                    _requeue_video(
                        video_id=vid,
                        channel_id=cid,
                        title=entry.get("title", ""),
                        date=date_str,
                        scheduled_start=scheduled_start,
                        next_try_at=next_try_at,
                        raw_entry_xml=entry.get("raw_entry_xml", ""),
                    )
                    resolved_ids.add(vid)
                    title = (
                        entry.get("title", "")
                        or _title_from_raw_xml(entry.get("raw_entry_xml", ""))
                        or "[title unknown]"
                    )
                    ch_name = feed_cfg.get("channel_name", "?")
                    logger.info(
                        f"WebSub: Livestream detected, re-queued for {next_try_at} - {vid}: {ch_name}: {title}"
                    )

                elif fetch_result == "permanent":
                    # Metadata already written by _wsb_try_fetch_transcript.
                    resolved_ids.add(vid)

                elif fetch_result == "transient":
                    # Schedule next attempt per the retry table.
                    attempts = entry.get("transcript_attempts", 0) + 1
                    queued_at_str = entry.get("queued_at")
                    if attempts >= _TRANSCRIPT_MAX_ATTEMPTS or not queued_at_str:
                        title = entry.get("title", "?")
                        ch_name = feed_cfg.get("channel_name", "?")
                        metadata_fmt = f"{vid}: {ch_name}: {title}"
                        msg = f"No transcript after {_TRANSCRIPT_MAX_ATTEMPTS} attempts, marking permanent"
                        logger.warning(f"WebSub: {msg} - {metadata_fmt}")
                        video_date = date_str[:10]
                        _write_no_transcript_metadata(
                            vid, feed_dir, video_date, entry.get("title", ""),
                            channel_name=feed_cfg.get("channel_name", ""),
                            video_published_at=entry.get("published_at", ""),
                        )
                        resolved_ids.add(vid)
                    else:
                        next_nta = _next_transcript_try(queued_at_str, attempts)
                        retry_updates.append({
                            **entry,
                            "transcript_attempts": attempts,
                            "next_try_at": next_nta,
                        })
                        title = entry.get("title", "?")
                        ch_name = feed_cfg.get("channel_name", "?")
                        metadata_fmt = f"{vid}: {ch_name}: {title}"
                        logger.debug(
                            f"WebSub: Transcript not ready (attempt {attempts}/{_TRANSCRIPT_MAX_ATTEMPTS}), "
                            f"next try at {next_nta} - {metadata_fmt}"
                        )

            # ----------------------------------------------------------------
            # Phase 2 — Gemini processing (capped by max_per_cycle)
            # ----------------------------------------------------------------
            # Group transcript-ready entries by channel so process_feed is called
            # once per channel (it handles focus-based multi-pass Gemini calls).
            channels_for_gemini: dict[str, list[dict]] = {}
            for entry in transcript_ready:
                channels_for_gemini.setdefault(entry["channel_id"], []).append(entry)

            if channels_for_gemini:
                n_videos = len(transcript_ready)
                n_channels = len(channels_for_gemini)
                logger.info(f"WebSub: {n_videos} transcript-ready video(s) across {n_channels} channel(s)")

            gemini_count = 0
            any_changed = False
            today_str = date.today().isoformat()

            for cid, entries in channels_for_gemini.items():
                if gemini_count >= max_per_cycle or ai_event.is_set():
                    # Defer remaining channels — their entries stay in the queue
                    # with transcript.txt cached, so Phase 1 will skip them next cycle.
                    break

                feed = channel_map[cid]
                video_infos = [
                    {
                        "id":    e["video_id"],
                        "title": (
                            e.get("title", "")
                            or _title_from_raw_xml(e.get("raw_entry_xml", ""))
                            or "[title unknown]"
                        ),
                        "date":        (e.get("date", "") or today_str)[:10],
                        "published_at": e.get("published_at", ""),
                        "_queue_entry": e,
                    }
                    for e in entries
                ]
                content_changed, error_type, _ = process_feed(
                    feed, supadata_client, current_config,
                    ai_event, transcript_event,
                    forced_videos=video_infos,  # type: ignore[arg-type]
                )
                if content_changed:
                    rebuild_feed(STORAGE_ROOT / slugify(feed["channel_name"]), feed)
                    any_changed = True
                if error_type and not ai_in_backoff:
                    if error_type == "service_unavailable":
                        backoff_secs = _AI_BACKOFF_SERVICE
                        msg = "service unavailable (503)"
                    elif error_type == "quota_exhausted_daily":
                        # Daily quota hit; back off 12 hours (quota resets sometime during day)
                        backoff_secs = _AI_BACKOFF_QUOTA_RPD
                        msg = "daily quota exhausted (429 RPD)"
                    else:  # "ai_rate_limited" (RPM)
                        backoff_secs = _AI_BACKOFF_QUOTA_RPM
                        msg = "per-minute rate limited (429 RPM)"
                    _ai_backoff_until = time.time() + backoff_secs
                    logger.warning(
                        f"WebSub: Gemini {msg} — backing off AI for {backoff_secs // 60}m {backoff_secs % 60}s"
                    )
                    # Stop processing more channels once we hit an error.
                    # Remaining channels' entries stay in the queue for next cycle.
                    break

                gemini_count += len(entries)

                # Determine which of this channel's entries are now resolved.
                feed_dir = STORAGE_ROOT / slugify(feed["channel_name"])
                for entry in entries:
                    evid = entry["video_id"]
                    if not _needs_processing(evid, feed_dir):
                        resolved_ids.add(evid)
                    elif ai_in_backoff or error_type:
                        # AI was unavailable this cycle (backoff active or just triggered).
                        # Don't charge a retry against the video — this is a Gemini
                        # availability issue, not a per-video failure.
                        retry_updates.append({**entry})
                    else:
                        # Gemini ran but failed or returned no output; use retry_count backstop.
                        rc = entry.get("retry_count", 0) + 1
                        if rc > _QUEUE_MAX_RETRIES:
                            logger.warning(
                                f"WebSub: Dropping video after {_QUEUE_MAX_RETRIES} Gemini-phase retries - {evid}"
                            )
                            resolved_ids.add(evid)
                        else:
                            retry_updates.append({**entry, "retry_count": rc})

            if any_changed or not (STORAGE_ROOT / "rss.xml").exists():
                try:
                    with _config_lock:
                        base_url = _daemon_config.get("base_url", "")
                    rebuild_aggregate_feed(base_url=base_url)
                except Exception as exc:
                    logger.error(
                        f"WebSub: Aggregate feed rebuild failed - {exc}",
                        exc_info=True,
                    )

            _remove_from_queue(resolved_ids)
            if retry_updates:
                _update_queue_entries(retry_updates)
            kept = len(retry_updates)
            done = len(resolved_ids)
            if kept:
                logger.info(f"WebSub: Resolved {done} video(s), kept {kept} for retry")
            else:
                logger.info(f"WebSub: Resolved {done} video(s)")

        except Exception as exc:
            logger.error(f"WebSub: Cycle failed - {exc}", exc_info=True)
        finally:
            _release_lock()

        # Heartbeat log every 5 minutes so we know the daemon is alive
        now = time.time()
        if now - _last_heartbeat >= _heartbeat_interval:
            logger.info("WebSub: Daemon alive and monitoring...")
            _last_heartbeat = now

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

    Thread Safety: The mtime check (_config_mtime) is intentionally not locked
    because it's an optimization, not correctness-critical. The actual
    _daemon_config update is protected by _config_lock. Worst case (benign
    race): reloading unchanged config. This avoids lock contention on hot path.

    Returns: The updated _daemon_config dict (same reference).
    """
    global _config_mtime

    config_file = Path(__file__).parent / "TubeNews.json"

    # Fast gate: check if file has been modified since last reload
    # NOTE: Reading _config_mtime without lock is safe (benign race); see docstring.
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
                    old_display, new_display = str(old), str(new)
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


def _setup_signal_handlers(ntfy_topic: str | None) -> None:
    """Set up signal handlers to notify when TubeNews is killed."""
    def _on_signal(signum: int, frame) -> None:
        sig_name = signal.Signals(signum).name
        msg = f"TubeNews daemon killed by signal {sig_name} ({signum})"
        logger.error(msg)
        if ntfy_topic:
            _send_ntfy_alert(ntfy_topic, "🚨 TubeNews Killed", msg, priority="urgent")
        raise SystemExit(128 + signum)

    # Catch termination signals
    for sig in [signal.SIGTERM, signal.SIGINT, signal.SIGHUP]:
        signal.signal(sig, _on_signal)


def _run_daemon(config: dict) -> None:
    """Start the WebSub daemon: subscribe all channels, then run the two threads.

    Thread 1 receives HTTP push payloads from YouTube's hub.
    Thread 2 wakes up periodically to process ripe queue entries and renew
    subscriptions.  Both are daemon threads — they exit when the process exits.

    This function blocks indefinitely (joins Thread 2).
    """
    global _daemon_config
    _daemon_config = config.copy()

    # Set up signal handlers for kill notifications
    ntfy_topic = config.get("ntfy_topic")
    if ntfy_topic:
        _setup_signal_handlers(ntfy_topic)
        logger.info(f"Kill notifications enabled via ntfy.sh/{ntfy_topic}")

    all_channels = _read_channels()
    channels = [ch for ch in all_channels if not ch.get("disabled", False)]
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


def _iter_all_users() -> Iterator[dict]:
    """Yield each user's raw data dict (from state/users/*/user.json).

    Yields dicts with an extra ``_uid`` key containing the UUID directory name.
    Skips unreadable or malformed files silently.
    """
    users_dir = STATE_ROOT / "users"
    if not users_dir.is_dir():
        return
    for user_dir in users_dir.iterdir():
        if not user_dir.is_dir():
            continue
        user_json = user_dir / "user.json"
        if not user_json.exists():
            continue
        try:
            data = json.loads(user_json.read_text(encoding="utf-8"))
            data["_uid"] = user_dir.name
            data["_user_dir"] = user_dir
            yield data
        except Exception:
            continue


def _scan_stories_since(user_data: dict, user_id: str, cutoff_ts: float) -> list[dict]:
    """Return story dicts (title, channel_name, video_id, start_seconds) for
    stories that are newer than *cutoff_ts* and visible to *user_id*.

    Only scans subscribed channels.  Respects ``**Users:**`` attribution.
    Results are sorted newest-first.
    """
    subscribed = set(user_data.get("channels", {}).keys())
    read_set = set(user_data.get("read_articles", []))
    raw: list[dict] = []
    for channel_dir in STORAGE_ROOT.iterdir():
        if not channel_dir.is_dir() or channel_dir.name.startswith("_"):
            continue
        channel_json = channel_dir / "channel.json"
        if not channel_json.exists():
            continue
        try:
            ch = json.loads(channel_json.read_text(encoding="utf-8"))
        except Exception:
            continue
        if ch.get("channel_id") not in subscribed:
            continue
        channel_name = ch.get("channel_name", channel_dir.name.replace("_", " "))
        for meeting_dir in channel_dir.iterdir():
            if not meeting_dir.is_dir():
                continue
            meta_path = meeting_dir / "metadata.json"
            if not meta_path.exists():
                continue
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
                if meta.get("status") == "ignored_too_old":
                    continue
                proc_at = _get_timestamp_as_float(meta.get("processed_at"))
                if proc_at <= cutoff_ts:
                    continue
                for story_file in sorted(meeting_dir.glob("[0-9]*.md")):
                    raw.append({"file": story_file, "meta": meta,
                                "channel_name": channel_name,
                                "processed_at": proc_at})
            except Exception:
                continue
    raw.sort(key=lambda e: e["processed_at"], reverse=True)

    stories = []
    for entry in raw:
        try:
            s = parse_story_file(entry["file"])
            story_user_ids = s.get("user_ids", [])
            if story_user_ids and user_id not in story_user_ids:
                continue
            if s.get("content_hash") in read_set:
                continue
            stories.append({
                "title": s["title"],
                "channel_name": entry["channel_name"],
                "video_id": entry["meta"]["video_id"],
                "start_seconds": s["start_seconds"],
                "content_hash": s.get("content_hash", ""),
                "file": entry["file"],  # Path; used by _select_podcast_stories
            })
        except Exception:
            continue
    return stories


def _build_digest_html(name: str, email: str, stories: list[dict], feed_url: str, base_url: str) -> str:
    """Build the HTML body for a daily digest email.

    *feed_url* must be an absolute URL (``base_url`` + ``/feed/<token>.html``).
    Each story links to ``feed_url#s<video_id>-<start_seconds>``.
    """
    import html as _html
    account_url = base_url.rstrip("/") + "/account" if base_url else ""
    # For email links, use public article URLs (don't require token or login)
    site_root = base_url.rstrip("/") if base_url else ""
    story_items = []
    for s in stories:
        # Generate public article URL: /article/{video_id}/{start_seconds}
        if site_root:
            href = f"{site_root}/article/{s['video_id']}/{s['start_seconds']}"
        else:
            # Fallback if no base_url: use relative path
            href = f"/article/{s['video_id']}/{s['start_seconds']}"
        title_escaped = _html.escape(s["title"])
        channel_escaped = _html.escape(s["channel_name"])
        story_items.append(
            '<li style="margin-bottom:0.5em">'
            f'<a href="{href}" style="color:#1a73e8;text-decoration:none">{title_escaped}</a>'
            f' <span style="color:#666;font-size:0.9em">— {channel_escaped}</span>'
            "</li>"
        )
    items_html = "\n".join(story_items)
    story_count = len(stories)
    story_word = "story" if story_count == 1 else "stories"
    footer = (
        '<p style="color:#888;font-size:0.85em;margin-top:2em">'
        "You're receiving this because you enabled daily email digests in TubeNews."
    )
    if account_url:
        footer += (
            f' <a href="{account_url}" style="color:#888">Manage preferences</a>.'
        )
    footer += "</p>"
    name_escaped = _html.escape(name)
    return f"""<!DOCTYPE html>
<html>
<head><meta charset="utf-8"></head>
<body style="font-family:Georgia,serif;max-width:600px;margin:0 auto;padding:1.5em;color:#111">
  <p>Hi {name_escaped}, here are your {story_count} new {story_word} from TubeNews:</p>
  <ul style="padding-left:1.2em;line-height:1.7">
{items_html}
  </ul>
  <p><a href="{feed_url}" style="color:#1a73e8">Open your full feed</a></p>
  <hr style="border:none;border-top:1px solid #ddd;margin:2em 0">
  {footer}
</body>
</html>"""


def _send_daily_digests(config: dict) -> None:
    """Send morning digest emails to opted-in users via Resend.

    Skips silently if ``resend_api_key`` is absent or ``base_url`` is not
    configured (relative URLs are unusable in email clients).
    """
    api_key = config.get("resend_api_key", "").strip()
    if not api_key:
        return
    base_url = config.get("base_url", "").rstrip("/")
    if not base_url:
        logger.warning(
            "Daily digest: base_url is not set in TubeNews.json"
            " — digest skipped (email links require an absolute URL)"
        )
        return
    from_email = config.get("resend_from_email", "TubeNews <noreply@example.com>")

    try:
        import resend as _resend
    except ImportError:
        logger.warning("Daily digest: 'resend' package not installed — run: pip install resend")
        return

    _resend.api_key = api_key

    sent = 0
    for user_data in _iter_all_users():
        if not user_data.get("preferences", {}).get("digest_email_enabled"):
            continue
        email = user_data.get("email", "")
        name = user_data.get("name", email)
        feed_token = user_data.get("feed_token", "")
        user_id = user_data.get("_uid", "")
        if not email or not feed_token:
            continue

        last_sent_iso = user_data.get("last_digest_sent")
        cutoff_ts: float
        if last_sent_iso:
            parsed = iso8601_to_unix(last_sent_iso)
            cutoff_ts = parsed if parsed is not None else time.time() - _SECONDS_PER_DAY
        else:
            cutoff_ts = time.time() - _SECONDS_PER_DAY

        stories = _scan_stories_since(user_data, user_id, cutoff_ts)
        if not stories:
            continue

        feed_url = f"{base_url}/feed/{feed_token}.html"
        html_body = _build_digest_html(name, email, stories, feed_url, base_url)
        story_count = len(stories)
        story_word = "story" if story_count == 1 else "stories"
        try:
            _resend.Emails.send({
                "from": from_email,
                "to": [email],
                "subject": f"Your TubeNews digest \u2014 {story_count} new {story_word}",
                "html": html_body,
            })
            # Update last_digest_sent in user.json
            user_dir: Path = user_data["_user_dir"]
            user_json_path = user_dir / "user.json"
            raw = json.loads(user_json_path.read_text(encoding="utf-8"))
            raw["last_digest_sent"] = now_utc_iso()
            tmp = user_json_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(raw, indent=2), encoding="utf-8")
            tmp.rename(user_json_path)
            logger.info(f"Daily digest sent to {email} ({story_count} {story_word})")
            sent += 1
        except Exception as exc:
            logger.warning(f"Daily digest: failed to send to {email}: {exc}")

    if sent:
        logger.info(f"Daily digest: sent to {sent} user{'s' if sent != 1 else ''}")


def _body_to_plain_text(body_html: str) -> str:
    """Convert body_html (HTML-escaped, <br>-joined) back to plain prose for TTS."""
    return html.unescape(body_html.replace("<br>", " ")).strip()


def _select_podcast_stories(
    user_data: dict, user_id: str, cutoff_ts: float
) -> list[dict]:
    """Select unread stories since *cutoff_ts* for podcast generation.

    Reuses ``_scan_stories_since`` for channel/attribution/timestamp filtering.
    Additionally skips stories already in ``read_articles``.  Accumulates
    stories newest-first until the total estimated word count reaches
    ``_PODCAST_TARGET_WORDS``.

    Returns a list of ``{"title", "channel_name", "body_text"}`` dicts.
    """
    candidates = _scan_stories_since(user_data, user_id, cutoff_ts)
    read_set = set(user_data.get("read_articles", []))
    selected: list[dict] = []
    total_words = 0
    for story in candidates:
        if story.get("content_hash") in read_set:
            continue
        try:
            parsed = parse_story_file(story["file"])
        except Exception:
            continue
        body_text = _body_to_plain_text(parsed.get("body_html", ""))
        if not body_text:
            continue
        word_count = len(body_text.split())
        selected.append({
            "title": story["title"],
            "channel_name": story["channel_name"],
            "body_text": body_text,
        })
        total_words += word_count
        if total_words >= _PODCAST_TARGET_WORDS:
            break
    return selected


def _generate_podcast_script(
    stories: list[dict], user_name: str, date_str: str, config: dict
) -> str | None:
    """Call Gemini to write a natural 10-minute podcast script from *stories*.

    Returns the script as plain text, or ``None`` on failure.
    Uses a simpler direct Gemini call (no JSON extraction) compared to
    :func:`call_gemini_api`.
    """
    api_key = config.get("gemini_api_key", "").strip()
    model = config.get("gemini_model", "gemini-2.0-flash")
    if not api_key:
        logger.warning("Podcast: gemini_api_key not set — cannot generate script")
        return None

    parts = []
    for i, story in enumerate(stories, 1):
        parts.append(
            f"STORY {i} \u2014 {story['title']} ({story['channel_name']})\n"
            f"{story['body_text']}"
        )
    formatted = "\n\n".join(parts)
    try:
        dt = datetime.strptime(date_str, "%Y-%m-%d")
        date_human = _fmt_no_leading_zeros(dt, "%B %-d, %Y")
    except (ValueError, TypeError):
        date_human = date_str

    prompt = (
        "You are a professional news podcast host. Write a natural, conversational "
        f"10-minute podcast script (approximately {_PODCAST_TARGET_WORDS} words) for "
        f"{user_name}'s TubeNews briefing on {date_human}.\n\n"
        "Format: brief 1-sentence welcome \u2192 one segment per story (introduce with a "
        "transition phrase, summarise in 3-5 sentences) \u2192 brief outro. "
        "No markdown, no headers. Plain spoken prose only. "
        "Do not mention \"TubeNews\" more than once. "
        "Keep a calm, authoritative news-anchor tone.\n\n"
        f"Stories to cover:\n\n{formatted}"
    )

    api_url = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        f"{model}:generateContent?key={api_key}"
    )
    payload = {"contents": [{"parts": [{"text": prompt}]}]}
    try:
        response = requests.post(api_url, json=payload, timeout=_GEMINI_TIMEOUT)
        if response.status_code != 200:
            logger.warning(
                "Podcast: Gemini returned HTTP %d while generating script",
                response.status_code,
            )
            return None
        candidates = response.json().get("candidates", [])
        if not candidates:
            logger.warning("Podcast: Gemini returned no candidates for script")
            return None
        parts_list = (
            candidates[0].get("content", {}).get("parts", [])
        )
        text = next((p.get("text", "") for p in parts_list if "text" in p), "")
        return text.strip() or None
    except Exception as exc:
        logger.warning("Podcast: Gemini script generation failed: %s", exc)
        return None


def _tts_google(text: str, api_key: str, voice: str, config: dict) -> bytes | None:
    """Synthesise *text* via Google Cloud Text-to-Speech REST API.  Returns MP3 bytes."""
    lang = config.get("tts_language_code", "en-US")
    voice_name = voice or "en-US-Neural2-J"
    url = f"https://texttospeech.googleapis.com/v1/text:synthesize?key={api_key}"
    payload = {
        "input": {"text": text},
        "voice": {"languageCode": lang, "name": voice_name},
        "audioConfig": {"audioEncoding": "MP3"},
    }
    try:
        resp = requests.post(url, json=payload, timeout=120)
        if resp.status_code != 200:
            logger.warning(
                "Podcast (google TTS): HTTP %d: %s",
                resp.status_code,
                resp.text[:120],
            )
            return None
        audio_b64 = resp.json().get("audioContent", "")
        if not audio_b64:
            logger.warning("Podcast (google TTS): empty audioContent in response")
            return None
        return base64.b64decode(audio_b64)
    except Exception as exc:
        logger.warning("Podcast (google TTS): request failed: %s", exc)
        return None


def _tts_deepgram(text: str, api_key: str, voice: str) -> bytes | None:
    """Synthesise *text* via Deepgram Aura TTS REST API.  Returns MP3 bytes."""
    model = voice or "aura-asteria-en"
    url = f"https://api.deepgram.com/v1/speak?model={model}"
    headers = {
        "Authorization": f"Token {api_key}",
        "Content-Type": "application/json",
    }
    try:
        resp = requests.post(url, json={"text": text}, headers=headers, timeout=120)
        if resp.status_code != 200:
            logger.warning(
                "Podcast (deepgram TTS): HTTP %d: %s",
                resp.status_code,
                resp.text[:120],
            )
            return None
        return resp.content
    except Exception as exc:
        logger.warning("Podcast (deepgram TTS): request failed: %s", exc)
        return None


def _tts_elevenlabs(text: str, api_key: str, voice: str) -> bytes | None:
    """Synthesise *text* via ElevenLabs TTS REST API.  Returns MP3 bytes."""
    voice_id = voice or "EXAVITQu4vr4xnSDxMaL"
    url = f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}"
    headers = {
        "xi-api-key": api_key,
        "Accept": "audio/mpeg",
        "Content-Type": "application/json",
    }
    payload = {"text": text, "model_id": "eleven_multilingual_v2"}
    try:
        resp = requests.post(url, json=payload, headers=headers, timeout=120)
        if resp.status_code != 200:
            logger.warning(
                "Podcast (elevenlabs TTS): HTTP %d: %s",
                resp.status_code,
                resp.text[:120],
            )
            return None
        return resp.content
    except Exception as exc:
        logger.warning("Podcast (elevenlabs TTS): request failed: %s", exc)
        return None


def _tts_openai(text: str, api_key: str, voice: str) -> bytes | None:
    """Synthesise *text* via OpenAI TTS REST API.  Returns MP3 bytes."""
    url = "https://api.openai.com/v1/audio/speech"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": "tts-1-hd",
        "input": text,
        "voice": voice or "nova",
        "response_format": "mp3",
    }
    try:
        resp = requests.post(url, json=payload, headers=headers, timeout=120)
        if resp.status_code != 200:
            logger.warning(
                "Podcast (openai TTS): HTTP %d: %s",
                resp.status_code,
                resp.text[:120],
            )
            return None
        return resp.content
    except Exception as exc:
        logger.warning("Podcast (openai TTS): request failed: %s", exc)
        return None


def _tts_synthesize(script_text: str, config: dict) -> bytes | None:
    """Dispatch to the configured TTS provider.  Returns MP3 bytes or ``None``."""
    provider = config.get("tts_provider", "google").lower()
    api_key = config.get("tts_api_key", "").strip()
    voice = config.get("tts_voice_id", "").strip()
    if not api_key:
        return None
    if provider == "google":
        return _tts_google(script_text, api_key, voice, config)
    if provider == "deepgram":
        return _tts_deepgram(script_text, api_key, voice)
    if provider == "elevenlabs":
        return _tts_elevenlabs(script_text, api_key, voice)
    if provider == "openai":
        return _tts_openai(script_text, api_key, voice)
    logger.warning("Podcast: unknown tts_provider %r — skipping synthesis", provider)
    return None


def _build_podcast_feed(user_data: dict, episodes: list[dict], base_url: str) -> bytes:
    """Build an iTunes-compatible podcast RSS feed from *episodes*.

    *episodes* is a list of sidecar dicts (newest-first):
    ``{"date": "YYYY-MM-DD", "title": str, "duration_seconds": int, "size_bytes": int}``
    """
    token = user_data.get("feed_token", "")
    name = user_data.get("name", "User")
    feed = FeedGenerator()
    feed.load_extension("podcast")
    feed.id(f"{base_url}/feed/{token}/podcast.xml")
    feed.title(f"{name}'s TubeNews Daily")
    feed.description("Daily AI-generated audio news briefing from TubeNews")
    feed.link(href=base_url if base_url else "https://tubenews.example")
    feed.language("en")
    feed.podcast.itunes_category("News")  # type: ignore[attr-defined]
    feed.podcast.itunes_author("TubeNews")  # type: ignore[attr-defined]
    feed.podcast.itunes_explicit("no")  # type: ignore[attr-defined]

    for ep in episodes:
        entry = feed.add_entry()
        mp3_url = f"{base_url}/feed/{token}/podcast/{ep['date']}.mp3"
        entry.id(mp3_url)
        entry.title(ep.get("title", f"TubeNews — {ep['date']}"))
        try:
            pub_dt = datetime.strptime(ep["date"], "%Y-%m-%d").replace(tzinfo=timezone.utc)
        except (ValueError, KeyError):
            pub_dt = datetime.now(timezone.utc)
        entry.published(pub_dt)
        entry.enclosure(mp3_url, str(ep.get("size_bytes", 0)), "audio/mpeg")
        entry.podcast.itunes_duration(  # type: ignore[attr-defined]
            str(ep.get("duration_seconds", 0))
        )

    return feed.rss_str(pretty=True)


def _generate_daily_podcasts(config: dict) -> None:
    """Generate a daily podcast episode for each opted-in user.

    Skips silently when ``tts_api_key`` or ``base_url`` is absent.  For each
    eligible user, selects unread stories since the last episode, writes a
    Gemini script, synthesises audio, saves the MP3 + JSON sidecar, purges
    old episodes beyond ``podcast_retention_days``, and rebuilds
    ``podcast.xml``.
    """
    tts_api_key = config.get("tts_api_key", "").strip()
    base_url = config.get("base_url", "").rstrip("/")
    if not tts_api_key or not base_url:
        return

    now = time.time()
    retention_days = int(config.get("podcast_retention_days", 7))
    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    generated = 0

    for user_data in _iter_all_users():
        if not user_data.get("preferences", {}).get("podcast_enabled"):
            continue

        email = user_data.get("email", "")
        user_dir: Path = user_data["_user_dir"]
        user_id: str = user_data["_uid"]
        podcast_dir = user_dir / "podcast"

        last_iso = user_data.get("last_podcast_generated")
        cutoff_ts: float
        if last_iso:
            parsed_ts = iso8601_to_unix(last_iso)
            cutoff_ts = parsed_ts if parsed_ts is not None else now - _SECONDS_PER_DAY
        else:
            cutoff_ts = now - _SECONDS_PER_DAY

        stories = _select_podcast_stories(user_data, user_id, cutoff_ts)
        if not stories:
            logger.info("Podcast: no new unread stories for %s — skipping", email)
            continue

        script = _generate_podcast_script(
            stories, user_data.get("name", ""), date_str, config
        )
        if not script:
            logger.warning("Podcast: script generation failed for %s", email)
            continue

        audio = _tts_synthesize(script, config)
        if not audio:
            logger.warning("Podcast: TTS synthesis failed for %s", email)
            continue

        # Write MP3 + JSON sidecar
        podcast_dir.mkdir(exist_ok=True)
        (podcast_dir / f"{date_str}.mp3").write_bytes(audio)
        word_count = len(script.split())
        duration_sec = max(60, round(word_count / 2.2))  # ~130 WPM
        try:
            ep_title = _fmt_no_leading_zeros(
                datetime.now(timezone.utc), "TubeNews \u2014 %B %-d, %Y"
            )
        except (ValueError, TypeError):
            ep_title = f"TubeNews \u2014 {date_str}"
        meta = {
            "date": date_str,
            "title": ep_title,
            "duration_seconds": duration_sec,
            "size_bytes": len(audio),
        }
        (podcast_dir / f"{date_str}.json").write_text(
            json.dumps(meta), encoding="utf-8"
        )

        # Purge episodes older than retention_days
        cutoff_date = (
            datetime.now(timezone.utc) - timedelta(days=retention_days)
        ).strftime("%Y-%m-%d")
        for old_mp3 in podcast_dir.glob("*.mp3"):
            if old_mp3.stem < cutoff_date:
                old_mp3.unlink(missing_ok=True)
                (podcast_dir / f"{old_mp3.stem}.json").unlink(missing_ok=True)

        # Rebuild podcast RSS
        episode_list: list[dict] = []
        for ep_json in podcast_dir.glob("*.json"):
            try:
                episode_list.append(
                    json.loads(ep_json.read_text(encoding="utf-8"))
                )
            except Exception:
                continue
        episode_list.sort(key=lambda e: e.get("date", ""), reverse=True)
        feed_bytes = _build_podcast_feed(user_data, episode_list, base_url)
        (user_dir / "podcast.xml").write_bytes(feed_bytes)

        # Persist last_podcast_generated
        user_json_path = user_dir / "user.json"
        raw = json.loads(user_json_path.read_text(encoding="utf-8"))
        raw["last_podcast_generated"] = now_utc_iso()
        tmp = user_json_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(raw, indent=2), encoding="utf-8")
        tmp.rename(user_json_path)

        logger.info(
            "Podcast: episode generated for %s (%d stories, %ds)",
            email, len(stories), duration_sec,
        )
        generated += 1

    if generated:
        logger.info("Podcast: generated %d episode%s", generated, "s" if generated != 1 else "")


def _send_ntfy(topic: str, total_stories: int, feed_results: list[FeedResult], started_at: str) -> None:
    """POST a run-summary notification to ntfy.sh/<topic>."""
    import urllib.request as _urllib_request

    started_dt = datetime.fromisoformat(started_at.replace("Z", "+00:00")).astimezone()
    timestamp = _fmt_no_leading_zeros(started_dt, "%B %d, %Y at %I:%M %p")
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


def _send_ntfy_alert(topic: str, title: str, message: str, priority: str = "high") -> None:
    """Send an alert notification to ntfy.sh/<topic>."""
    import urllib.request as _urllib_request

    req = _urllib_request.Request(
        f"https://ntfy.sh/{topic}",
        data=message.encode(),
        method="POST",
        headers={"Title": title, "Priority": priority},
    )
    try:
        _urllib_request.urlopen(req, timeout=5)
        logger.debug(f"ntfy.sh/{topic}: alert sent")
    except Exception as exc:
        logger.warning(f"ntfy.sh/{topic}: alert failed: {exc}")


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
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
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
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as config_file:
            config = json.load(config_file)
    except FileNotFoundError:
        logger.error(
            f"TubeNews: Configuration file not found: {CONFIG_FILE}\n"
            f"Copy TubeNews.json.sample to {CONFIG_FILE} and fill in your API keys."
        )
        return
    except json.JSONDecodeError as exc:
        logger.error(
            f"TubeNews: Configuration file {CONFIG_FILE} contains invalid JSON: {exc}"
        )
        return
    except Exception as exc:
        logger.error(f"TubeNews: Failed to load configuration: {exc}")
        return

    try:
        _validate_config(config)
    except ValueError as exc:
        logger.error(str(exc))
        return

    all_channels = _read_channels()
    channels = [ch for ch in all_channels if not ch.get("disabled", False)]
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
    logger.info(
        f"Session Start | {_fmt_no_leading_zeros(datetime.now(), '%A, %B %d, %Y')}"
        f" | AI Model: {config.get('gemini_model')}"
    )

    # Check cached Supadata balance before doing any work.
    quota_ok, cached_balance = _check_supadata_quota(config)
    started_at = now_utc_iso()
    if not quota_ok:
        run_log_path = STATE_ROOT / "run_logs" / "run_log.json"
        run_log_path.parent.mkdir(exist_ok=True)
        try:
            runs = json.loads(run_log_path.read_text()) if run_log_path.exists() else []
        except Exception as exc:
            logger.warning(f"run_log.json is corrupted, starting fresh: {exc}")
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
        except Exception as exc:
            logger.error(f"TubeNews: Meta feed rebuild failed: {exc}", exc_info=True)

    users_dir = STATE_ROOT / "users"
    if users_dir.is_dir():
        for user_json in sorted(users_dir.glob("*/user.json")):
            try:
                user = json.loads(user_json.read_text())
                uid = user_json.parent.name
                rebuild_user_feed(user, base_url=config.get("base_url", ""), user_id=uid)
            except Exception as exc:
                logger.warning(f"TubeNews: Failed to rebuild feed for user {user_json.parent.name}: {exc}")

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
    if remaining <= 0 < max_credits:
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
