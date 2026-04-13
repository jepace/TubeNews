"""TubeNews web UI — account management and feed subscription.

Start the server (always use gunicorn — never python web/app.py):
    ./serve.sh

With HTTPS (behind nginx/Caddy):
    TUBENEWS_HTTPS=true ./serve.sh

The secret key is read from the "tubenews_key" field in TubeNews.json.
Generate one with: python -c 'import secrets; print(secrets.token_hex(32))'
"""

import html
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import time
import uuid

from datetime import datetime, timezone
from functools import wraps
from pathlib import Path
from typing import TypedDict

import pytz

from flask import (
    Flask,
    Response,
    abort,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    send_file,
    send_from_directory,
    url_for,
)
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from flask_login import (
    LoginManager,
    UserMixin,
    current_user,
    login_required,
    login_user,
    logout_user,
)
from flask_wtf.csrf import CSRFProtect
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.security import check_password_hash, generate_password_hash

# ---------------------------------------------------------------------------
# Path setup — import from TubeNews.py in the parent directory
# ---------------------------------------------------------------------------

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

from TubeNews import (  # noqa: E402
    STORAGE_ROOT,
    STATE_ROOT,
    FeedConfig,
    ParsedStory,
    parse_story_file,
    build_user_feed_xml,
    slugify,
    rebuild_feed,
    rebuild_aggregate_feed,
    _wsb_subscribe,
    _wsb_unsubscribe,
    now_utc_iso,
    _get_timestamp_as_float,
    _fmt_no_leading_zeros,
)

CONFIG_FILE = BASE_DIR / "TubeNews.json"
USERS_ROOT = STATE_ROOT / "users"
LOCK_FILE = STATE_ROOT / ".tubenews.lock"
TUBENEWS_PY = BASE_DIR / "TubeNews.py"

# Path-component validation for comment routes.
_SAFE_SLUG_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_-]*$')
_STORY_FILE_RE = re.compile(r'^\d{2}_[A-Za-z0-9_]+\.md$')

# ---------------------------------------------------------------------------
# App setup
# ---------------------------------------------------------------------------

app = Flask(__name__)
# Trust one level of X-Forwarded-For / X-Forwarded-Proto from the reverse
# proxy (nginx/Caddy) so rate limiting and IP logging see real client IPs.
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
logger = logging.getLogger(__name__)

try:
    _cfg = json.loads(CONFIG_FILE.read_text())
    secret_key = _cfg.get("tubenews_key") or os.environ.get("TUBENEWS_SECRET_KEY")
    _port = int(_cfg.get("port", 8000))
except Exception:
    secret_key = os.environ.get("TUBENEWS_SECRET_KEY")
    _port = 8000
if not secret_key:
    raise RuntimeError(
        "'tubenews_key' is not set in TubeNews.json. "
        "Generate one with: python -c 'import secrets; print(secrets.token_hex(32))'"
    )
app.config["SECRET_KEY"] = secret_key
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_SECURE"] = os.environ.get("TUBENEWS_HTTPS", "").lower() in ("1", "true", "yes")
app.config["REMEMBER_COOKIE_DURATION"] = 60 * 60 * 24 * 30  # 30 days

csrf = CSRFProtect(app)
login_manager = LoginManager(app)
login_manager.login_view = "login"
login_manager.login_message = "Please log in to access that page."

limiter = Limiter(
    get_remote_address,
    app=app,
    default_limits=[],
    storage_uri="memory://",
)

# ---------------------------------------------------------------------------
# User model
# ---------------------------------------------------------------------------


class User(UserMixin):
    """Thin wrapper around user.json so flask-login can work with it."""

    def __init__(self, user_dir: Path, data: dict):
        self._dir = user_dir
        self._data = data

    def get_id(self) -> str:
        return self._dir.name  # UUID directory name

    @property
    def email(self) -> str:
        return self._data["email"]

    @property
    def name(self) -> str:
        return self._data.get("name", self._data["email"].split("@")[0])

    @property
    def channel_ids(self) -> list[str]:
        return list(self._data.get("channels", {}).keys())

    @property
    def feed_token(self) -> str:
        return self._data["feed_token"]

    @property
    def is_locked(self) -> bool:
        return bool(self._data.get("locked", False))

    @property
    def is_admin(self) -> bool:
        try:
            cfg = json.loads(CONFIG_FILE.read_text())
            return self.email in [e.strip().lower() for e in cfg.get("admin_users", [])]
        except Exception:
            return False

    # flask-login: locked accounts are treated as inactive
    @property
    def is_active(self) -> bool:
        return not self.is_locked

    def _save(self) -> None:
        tmp = self._dir / "user.json.tmp"
        tmp.write_text(json.dumps(self._data, indent=2))
        tmp.rename(self._dir / "user.json")


# ---------------------------------------------------------------------------
# Data contracts (TypedDicts)
# ---------------------------------------------------------------------------


class ChannelInfo(TypedDict):
    """Minimal channel descriptor returned by :func:`_channel_info_for_dir`."""
    channel_id: str
    channel_name: str


class ChannelStat(TypedDict):
    """Per-channel archive statistics returned by :func:`_archive_channel_stats`."""
    channel_id: str
    channel_name: str
    processed: int
    ignored: int
    no_stories: int
    story_count: int
    last_processed: float


class StoryDict(TypedDict, total=False):
    """Fully-resolved story dict served to Flask templates and the feed builders."""
    title: str
    dateline: str
    body_html: str
    start_seconds: int
    video_id: str
    video_title: str
    channel_name: str
    channel_slug: str
    meeting_id: str
    story_filename: str
    processed_at: float
    content_hash: str
    channel_id: str
    published: str
    comment_count: int
    user_ids: list[str]


# ---------------------------------------------------------------------------
# User lookup helpers
# ---------------------------------------------------------------------------


def _read_email_index() -> dict[str, str]:
    """Return the email→uuid mapping from USERS_ROOT/index.json, or {} on failure."""
    try:
        return json.loads((USERS_ROOT / "index.json").read_text())
    except Exception:
        return {}


def _write_email_index(index: dict[str, str]) -> None:
    """Atomically write *index* (email→uuid) to USERS_ROOT/index.json."""
    USERS_ROOT.mkdir(parents=True, exist_ok=True)
    tmp = USERS_ROOT / "index.json.tmp"
    tmp.write_text(json.dumps(index, indent=2))
    tmp.replace(USERS_ROOT / "index.json")


def _index_add(email: str, uid: str) -> None:
    index = _read_email_index()
    index[email.strip().lower()] = uid
    _write_email_index(index)


def _index_remove(email: str) -> None:
    index = _read_email_index()
    index.pop(email.strip().lower(), None)
    _write_email_index(index)


def _find_user_by_email(email: str) -> User | None:
    if not USERS_ROOT.is_dir():
        return None
    needle = email.strip().lower()

    # Fast path: O(1) index lookup.
    index = _read_email_index()
    if needle in index:
        user = _find_user_by_id(index[needle])
        if user and user.email.lower() == needle:
            return user
        # Index entry is stale — fall through to glob scan.

    # Slow path: glob scan (first run after upgrade, or index corruption recovery).
    for user_json in USERS_ROOT.glob("*/user.json"):
        try:
            data = json.loads(user_json.read_text())
            if data.get("email", "").lower() == needle:
                user = User(user_json.parent, data)
                _index_add(needle, user_json.parent.name)  # repair index
                return user
        except Exception as exc:
            logger.debug(f"Skipping {user_json.parent.name}: {exc}")
            continue
    return None


def _find_user_by_id(user_id: str) -> User | None:
    user_json = USERS_ROOT / user_id / "user.json"
    if not user_json.exists():
        return None
    try:
        return User(user_json.parent, json.loads(user_json.read_text()))
    except Exception:
        return None


def _all_users() -> list[User]:
    if not USERS_ROOT.is_dir():
        return []
    users = []
    for user_json in sorted(USERS_ROOT.glob("*/user.json")):
        try:
            users.append(User(user_json.parent, json.loads(user_json.read_text())))
        except Exception as exc:
            logger.debug(f"Skipping {user_json.parent.name}: {exc}")
            continue
    return sorted(users, key=lambda u: u.name.lower())


@login_manager.user_loader
def load_user(user_id: str) -> User | None:
    return _find_user_by_id(user_id)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _web_ntfy(title: str, message: str, priority: str = "default") -> None:
    """Send a web-event notification to ntfy.sh if ntfy_topic is configured.

    Uses the same topic as the CLI run-summary notifications.  Failures are
    silently swallowed — notifications are best-effort and must never break
    a web request.
    """
    topic = _load_config().get("ntfy_topic")
    if not topic:
        return
    import urllib.request as _ur
    req = _ur.Request(
        f"https://ntfy.sh/{topic}",
        data=message.encode(),
        method="POST",
        headers={"Title": title, "Priority": priority},
    )
    try:
        _ur.urlopen(req, timeout=5)
    except Exception:
        pass


def _is_running() -> bool:
    """Return True if a TubeNews.py process currently holds the lock file."""
    if not LOCK_FILE.exists():
        return False
    try:
        pid = int(LOCK_FILE.read_text().strip())
        os.kill(pid, 0)   # signal 0 = existence check only
        return True
    except (ValueError, ProcessLookupError, PermissionError):
        return False


def _load_config() -> dict:
    try:
        return json.loads(CONFIG_FILE.read_text())
    except Exception:
        return {}


def _save_channels(feeds: list[FeedConfig]) -> None:
    """Atomically write the channel list to ``state/channels.json``."""
    STATE_ROOT.mkdir(parents=True, exist_ok=True)
    sorted_feeds = sorted(feeds, key=lambda ch: ch.get("channel_name", "").lower())
    tmp = STATE_ROOT / "channels.json.tmp"
    tmp.write_text(json.dumps(sorted_feeds, indent=2))
    tmp.replace(STATE_ROOT / "channels.json")


def _load_channels() -> list[FeedConfig]:
    """Return configured channels, reading from ``state/channels.json``.

    Falls back to ``feeds[]`` in ``TubeNews.json`` for backward compatibility
    with installs that have not yet been migrated.
    """
    channels_file = STATE_ROOT / "channels.json"
    if channels_file.exists():
        try:
            return json.loads(channels_file.read_text())
        except Exception:
            pass
    try:
        return json.loads(CONFIG_FILE.read_text()).get("feeds", [])
    except Exception:
        return []


def _base_url() -> str:
    try:
        return json.loads(CONFIG_FILE.read_text()).get("base_url", "").rstrip("/")
    except Exception:
        return ""


def _get_user_timezone(user) -> str:
    """Get user's timezone, fallback to UTC."""
    if user and user._data.get("preferences", {}).get("timezone"):
        return user._data["preferences"]["timezone"]
    return "UTC"


def _fmt_video_date(date_str: str) -> str:
    """Format a YYYY-MM-DD video date as 'Video published Month D, YYYY'.

    Returns the original string on parse failure, empty string if input is empty.
    """
    if not date_str:
        return ""
    try:
        dt = datetime.strptime(date_str, "%Y-%m-%d")
        return f"Video published {dt.strftime('%B')} {dt.day}, {dt.year}"
    except (ValueError, TypeError):
        return date_str


def _reformat_published_timestamp(published_str: str, user_timezone: str, server_timezone: str = "") -> str:
    """Parse published timestamp string and reformat to user's timezone.

    Input format: "April 5, 2026 at 3:15 PM EST"
    Output format: "April 5, 2026 at 12:15 PM PDT" (reformatted to user TZ)

    Falls back to original string if parsing fails.

    Args:
        published_str: The published timestamp string to convert.
        user_timezone: Target IANA timezone (e.g., "America/Los_Angeles").
        server_timezone: Original IANA timezone the timestamp was written in.
                        Defaults to system timezone if not provided.
    """
    if not published_str or not user_timezone:
        return published_str

    try:
        import re
        # Parse: "April 5, 2026 at 3:15 PM EST"
        match = re.match(
            r"(\w+)\s+(\d+),\s+(\d+)\s+at\s+(\d+):(\d+)\s+(AM|PM)\s+(\w+)$",
            published_str
        )
        if not match:
            return published_str

        month_str, day_str, year_str, hour_str, min_str, ampm_str, _ = match.groups()

        # Use provided server timezone or fall back to configured timezone
        orig_tz_name = server_timezone or "UTC"
        try:
            orig_tz = pytz.timezone(orig_tz_name)
        except Exception:
            return published_str

        # Convert month name to number
        from datetime import datetime as dt_class
        month_num = dt_class.strptime(month_str, "%B").month

        # Build datetime in original timezone
        hour = int(hour_str)
        if ampm_str == "PM" and hour != 12:
            hour += 12
        elif ampm_str == "AM" and hour == 12:
            hour = 0

        orig_dt = orig_tz.localize(
            dt_class(int(year_str), month_num, int(day_str), hour, int(min_str), 0)
        )

        # Convert to user's timezone
        user_tz = pytz.timezone(user_timezone)
        user_dt = orig_dt.astimezone(user_tz)

        # Format with _fmt_no_leading_zeros
        formatted_date = _fmt_no_leading_zeros(user_dt, "%B %d, %Y")
        formatted_time = _fmt_no_leading_zeros(user_dt, "%I:%M %p")
        tz_abbr = user_dt.strftime("%Z")

        return f"{formatted_date} at {formatted_time} {tz_abbr}"
    except Exception:
        return published_str


def _rss_url(token: str) -> str:
    base = _base_url()
    if base:
        return f"{base}/feed/{token}.xml"
    return f"/feed/{token}.xml"


def _feed_url(token: str) -> str:
    base = _base_url()
    if base:
        return f"{base}/feed/{token}.html"
    return f"/feed/{token}.html"


def _prefs_to_classes(prefs: dict) -> str:
    """Convert a user preferences dict to an HTML class string for <html>."""
    classes = []
    if prefs.get("dark_mode"):
        classes.append("dark")
    fs = prefs.get("font_size", "normal")
    if fs in ("large", "larger"):
        classes.append(f"font-{fs}")
    return " ".join(classes)


@app.context_processor
def inject_body_classes():
    """Make body_classes and unseen_channel_count available in every template."""
    if current_user.is_authenticated:
        classes = _prefs_to_classes(current_user._data.get("preferences", {}))
        # Only compute unseen count when seen_channel_ids has been initialised;
        # absent key means existing user pre-deploy — show nothing to avoid noise.
        if "seen_channel_ids" in current_user._data:
            all_ids = {ch["channel_id"] for ch in _load_channels()}
            seen_ids = set(current_user._data["seen_channel_ids"])
            unseen_count = len(all_ids - seen_ids)
        else:
            unseen_count = 0
        # Debounced last_accessed update — at most one disk write per 5 minutes.
        # Update on first access (key absent) or if 5+ minutes have passed since last update.
        now = time.time()
        if "last_accessed" not in current_user._data:
            # First access: set the timestamp immediately
            current_user._data["last_accessed"] = now_utc_iso()
            current_user._save()
        else:
            # Subsequent accesses: update only if 5+ minutes have passed
            last_accessed_ts = _get_timestamp_as_float(current_user._data.get("last_accessed"))
            if now - last_accessed_ts > 300:
                current_user._data["last_accessed"] = now_utc_iso()
                current_user._save()
        feed_name = current_user._data.get("feed_name") or f"{current_user.name}'s TubeNews"
    else:
        classes = ""
        unseen_count = 0
        feed_name = "TubeNews"
    return {"body_classes": classes, "unseen_channel_count": unseen_count, "feed_name": feed_name}


@app.template_filter("format_ts")
def format_ts(ts: int | str | None) -> str:
    if not ts:
        return "—"
    ts_float = _get_timestamp_as_float(ts)
    tz_name = _get_user_timezone(current_user)
    try:
        tz = pytz.timezone(tz_name)
        dt = datetime.fromtimestamp(ts_float, tz=timezone.utc).astimezone(tz)
        return dt.strftime("%Y-%m-%d")
    except Exception:
        # Fallback to UTC if timezone is invalid
        return datetime.fromtimestamp(ts_float, tz=timezone.utc).strftime("%Y-%m-%d")


@app.template_filter("format_datetime")
def format_datetime(ts: int | str | None) -> str:
    if not ts:
        return "—"
    ts_float = _get_timestamp_as_float(ts)
    tz_name = _get_user_timezone(current_user)
    try:
        tz = pytz.timezone(tz_name)
        dt = datetime.fromtimestamp(ts_float, tz=timezone.utc).astimezone(tz)
        tz_abbr = dt.strftime("%Z")
        return dt.strftime("%Y-%m-%d %H:%M") + f" {tz_abbr}"
    except Exception:
        # Fallback to UTC if timezone is invalid
        return datetime.fromtimestamp(ts_float, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _sanitize_focus(text: str) -> str:
    """Sanitize a user-supplied focus line against prompt injection.

    Allows only ASCII letters, digits, spaces, commas, and hyphens.
    Strips everything else (prevents URLs and Unicode homographs),
    collapses runs of whitespace to a single space, and truncates to 100 characters.
    """
    cleaned = re.sub(r"[^a-zA-Z0-9\s,\-]", "", text)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned[:100]


@app.template_filter("focuses_text")
def focuses_text(val) -> str:
    """Render a channel_focus list[str] as newline-separated text."""
    if not val:
        return ""
    return "\n".join(val)


@app.template_filter("highlight")
def highlight_filter(text: str, query: str) -> str:
    """HTML-escape *text* and wrap each occurrence of *query* in <mark>."""
    escaped = html.escape(text)
    if not query:
        return escaped
    pattern = re.compile(re.escape(query), re.IGNORECASE)
    return pattern.sub(lambda m: f"<mark>{html.escape(m.group())}</mark>", escaped)


def admin_required(f):
    """Decorator: 403 unless the logged-in user is an admin."""
    @wraps(f)
    def decorated(*args, **kwargs):
        if not current_user.is_authenticated or not current_user.is_admin:
            abort(403)
        return f(*args, **kwargs)
    return decorated


def _safe_next(url: str | None) -> str:
    """Return *url* only when it is a safe same-site relative path.

    Blocks absolute URLs (http://evil.com) and protocol-relative URLs
    (//evil.com) that would cause an open redirect after login.
    """
    if url and url.startswith("/") and not url.startswith("//"):
        return url
    return url_for("serve_feed")


def _channel_info_for_dir(channel_dir: Path) -> ChannelInfo | None:
    """Return ``{channel_id, channel_name}`` for *channel_dir*, or None."""
    channel_json = channel_dir / "channel.json"
    try:
        return json.loads(channel_json.read_text())
    except Exception:
        return None


def _find_archive_dir_for_channel(channel_id: str) -> Path | None:
    """Return the archive directory whose channel.json matches *channel_id*, or None."""
    if not STORAGE_ROOT.is_dir():
        return None
    for d in STORAGE_ROOT.iterdir():
        if not d.is_dir() or d.name == "users" or d.name.startswith("_"):
            continue
        cj = d / "channel.json"
        if cj.exists():
            try:
                if json.loads(cj.read_text()).get("channel_id") == channel_id:
                    return d
            except Exception as exc:
                logger.debug(f"Skipping {d}: {exc}")
    return None


def _archive_channel_stats() -> list[ChannelStat]:
    """Scan archive dirs and return per-channel processing stats."""
    stats = []
    if not STORAGE_ROOT.is_dir():
        return stats
    for channel_dir in STORAGE_ROOT.iterdir():
        if not channel_dir.is_dir() or channel_dir.name == "users" or channel_dir.name.startswith("_"):
            continue
        info = _channel_info_for_dir(channel_dir)
        if not info:
            continue
        processed = ignored = no_stories = story_count = 0
        last_processed = 0
        for meta_file in channel_dir.glob("*/metadata.json"):
            try:
                meta = json.loads(meta_file.read_text())
                status = meta.get("status")
                if status == "processed":
                    processed += 1
                    story_count += len(list(meta_file.parent.glob("[0-9]*.md")))
                    last_processed = max(last_processed, _get_timestamp_as_float(meta.get("processed_at")))
                elif status == "ignored_too_old":
                    ignored += 1
                elif status == "no_stories":
                    no_stories += 1
            except Exception as exc:
                logger.debug(f"Skipping {meta_file}: {exc}")
                continue
        stats.append({
            "channel_id": info.get("channel_id", ""),
            "channel_name": info.get("channel_name", channel_dir.name),
            "processed": processed,
            "ignored": ignored,
            "no_stories": no_stories,
            "story_count": story_count,
            "last_processed": last_processed,
        })
    return sorted(stats, key=lambda s: s["channel_name"].lower())


def _channel_counts(stories: list[StoryDict]) -> list[dict]:
    """Return [{channel_id, channel_name, count}] sorted by count descending."""
    mapping: dict[str, dict] = {}
    for s in stories:
        cid = s["channel_id"]
        if cid not in mapping:
            mapping[cid] = {"channel_id": cid, "channel_name": s["channel_name"], "count": 0}
        mapping[cid]["count"] += 1
    return sorted(mapping.values(), key=lambda c: c["channel_name"].lower())


def _user_bundles(user_data: dict) -> list[dict]:
    """Return the user's bundles with a 'slug' field added (computed from name, lowercased)."""
    return [
        {"name": b["name"], "slug": slugify(b["name"]).lower(), "channel_ids": b.get("channel_ids", [])}
        for b in user_data.get("bundles", [])
        if b.get("name", "").strip()
    ]


def _bundle_counts(stories: list[StoryDict], bundles: list[dict]) -> list[dict]:
    """Add a 'count' of matching stories to each bundle dict."""
    result = []
    for b in bundles:
        cids = set(b["channel_ids"])
        count = sum(1 for s in stories if s["channel_id"] in cids)
        result.append({**b, "count": count})
    return result


def _story_comment_count(story_file: Path) -> int:
    """Return the number of comments for a story file, or 0 if none."""
    comment_file = story_file.with_name(story_file.stem + "_comments.json")
    if not comment_file.exists():
        return 0
    try:
        return len(json.loads(comment_file.read_text(encoding="utf-8")))
    except Exception:
        return 0


def _get_channel_stories(channel_id: str, user_timezone: str = "") -> tuple[str | None, list[StoryDict]]:
    """Return (channel_name, stories) for a single channel, newest-first.

    All processed stories are returned with no time cutoff — this is a full
    archive browse, not a recency-filtered feed view.  Returns (None, []) if
    no matching channel archive is found.

    Args:
        channel_id: The channel ID to fetch stories for.
        user_timezone: User's timezone preference (IANA format). Falls back to
                       system timezone if not provided.
    """
    if not STORAGE_ROOT.is_dir():
        return None, []
    for channel_dir in STORAGE_ROOT.iterdir():
        if not channel_dir.is_dir() or channel_dir.name == "users" or channel_dir.name.startswith("_"):
            continue
        channel_info = _channel_info_for_dir(channel_dir)
        if not channel_info or channel_info.get("channel_id") != channel_id:
            continue
        channel_name = channel_info.get("channel_name", channel_dir.name.replace("_", " "))
        raw = []
        for meeting_dir in [d for d in channel_dir.iterdir() if d.is_dir()]:
            meta_path = meeting_dir / "metadata.json"
            if not meta_path.exists():
                continue
            try:
                meta = json.loads(meta_path.read_text())
                if meta.get("status") != "processed":
                    continue
                for story_file in meeting_dir.glob("[0-9]*.md"):
                    raw.append({"file": story_file, "meta": meta, "channel_name": channel_name,
                                "channel_id": channel_id,
                                "channel_slug": channel_dir.name, "meeting_id": meeting_dir.name})
            except Exception as exc:
                logger.debug(f"Skipping {meeting_dir}: {exc}")
                continue
        raw.sort(key=lambda e: _get_timestamp_as_float(e["meta"].get("processed_at")), reverse=True)
        stories = []
        for entry in raw:
            try:
                s = parse_story_file(entry["file"])
                vid = entry["meta"]["video_id"]
                vt = entry["meta"].get("video_title", "")
                published = s.get("published", "")
                # Reformat published timestamp to user's timezone if present
                if published:
                    tz = user_timezone or "UTC"
                    published = _reformat_published_timestamp(published, tz, "UTC")
                stories.append({
                    "title": s["title"],
                    "dateline": s["dateline"],
                    "video_date": _fmt_video_date(entry["meta"].get("video_date", "")),
                    "body_html": s["body_html"],
                    "start_seconds": s["start_seconds"],
                    "video_id": vid,
                    "video_title": vt if vt != vid else "",
                    "channel_name": entry["channel_name"],
                    "channel_slug": entry.get("channel_slug", ""),
                    "meeting_id": entry.get("meeting_id", ""),
                    "story_filename": entry["file"].name,
                    "processed_at": _get_timestamp_as_float(entry["meta"].get("processed_at")),
                    "channel_id": channel_id,
                    "published": published,
                    "comment_count": _story_comment_count(entry["file"]),
                })
            except Exception as exc:
                logger.debug(f"Skipping {entry['file']}: {exc}")
                continue
        return channel_name, stories
    return None, []


def _get_user_stories(user_data: dict, user_id: str = "") -> list[StoryDict]:
    """Return parsed stories for a user's subscribed channels, newest-first.

    Stories are filtered by ``user_id``: if a story's ``**Users:**`` line is
    present, it is shown only to the listed users.  Stories without a
    ``**Users:**`` line (feed-level or legacy) are shown to everyone.
    """
    subscribed = set(user_data.get("channels", {}).keys())
    raw: list[dict] = []
    for channel_dir in [d for d in STORAGE_ROOT.iterdir()
                        if d.is_dir() and d.name != "users" and not d.name.startswith("_")]:
        channel_info = _channel_info_for_dir(channel_dir)
        if not channel_info or channel_info.get("channel_id") not in subscribed:
            continue
        channel_id = channel_info.get("channel_id", "")
        channel_name = channel_info.get("channel_name", channel_dir.name.replace("_", " "))
        for meeting_dir in [d for d in channel_dir.iterdir() if d.is_dir()]:
            meta_path = meeting_dir / "metadata.json"
            if not meta_path.exists():
                continue
            try:
                meta = json.loads(meta_path.read_text())
                if meta.get("status") == "ignored_too_old":
                    continue
                for story_file in meeting_dir.glob("[0-9]*.md"):
                    raw.append({"file": story_file, "meta": meta, "channel_name": channel_name,
                                "channel_id": channel_id,
                                "channel_slug": channel_dir.name, "meeting_id": meeting_dir.name})
            except Exception as exc:
                logger.debug(f"Skipping {meeting_dir}: {exc}")
                continue
    raw.sort(key=lambda e: _get_timestamp_as_float(e["meta"].get("processed_at")), reverse=True)
    stories = []
    for entry in raw:
        try:
            s = parse_story_file(entry["file"])
            story_user_ids = s.get("user_ids", [])
            if story_user_ids and user_id not in story_user_ids:
                continue
            vid = entry["meta"]["video_id"]
            vt = entry["meta"].get("video_title", "")
            # Reformat published timestamp to user's timezone if present
            published = s.get("published", "")
            if published:
                user_tz = user_data.get("preferences", {}).get("timezone", "UTC")
                published = _reformat_published_timestamp(published, user_tz, "UTC")
            stories.append({
                "title": s["title"],
                "dateline": s["dateline"],
                "video_date": _fmt_video_date(entry["meta"].get("video_date", "")),
                "body_html": s["body_html"],
                "start_seconds": s["start_seconds"],
                "video_id": vid,
                "video_title": vt if vt != vid else "",
                "channel_name": entry["channel_name"],
                "channel_slug": entry.get("channel_slug", ""),
                "meeting_id": entry.get("meeting_id", ""),
                "story_filename": entry["file"].name,
                "processed_at": _get_timestamp_as_float(entry["meta"].get("processed_at")),
                "content_hash": s.get("content_hash", ""),
                "channel_id": entry["channel_id"],
                "published": published,
                "comment_count": _story_comment_count(entry["file"]),
            })
        except Exception as exc:
            logger.debug(f"Skipping {entry['file']}: {exc}")
            continue
    return stories


# ---------------------------------------------------------------------------
# Public routes
# ---------------------------------------------------------------------------


@app.route("/")
def index():
    if current_user.is_authenticated:
        return redirect(url_for("serve_feed"))
    return redirect(url_for("login"))


@app.route("/content/")
@app.route("/content/<path:filename>")
def serve_content(filename=""):
    """Serve static files from the content directory (feeds, stories, etc.)."""
    if not filename:
        abort(404)
    # Never expose internal reserved directories (including _users/).
    if filename.startswith("_"):
        abort(404)
    # Guard against path traversal: ensure the resolved target stays inside STORAGE_ROOT.
    safe_root = STORAGE_ROOT.resolve()
    target = (STORAGE_ROOT / filename).resolve()
    if not str(target).startswith(str(safe_root) + os.sep) and target != safe_root:
        abort(404)
    mimetype = "application/rss+xml" if filename.endswith(".xml") else None
    return send_from_directory(STORAGE_ROOT, filename, mimetype=mimetype)


@app.route("/transcript/<channel_slug>/<meeting_id>")
def serve_transcript(channel_slug, meeting_id):
    """Render a transcript as a readable HTML page with per-segment anchors.

    URL fragment ``#t<seconds>`` scrolls to (and highlights) the matching
    segment, e.g. ``/transcript/my_channel/2026-03-14_abc123#t120``.
    """
    import re as _re
    # Guard against path traversal: verify the resolved path stays inside STORAGE_ROOT
    try:
        meeting_dir = (STORAGE_ROOT / channel_slug / meeting_id).resolve()
        meeting_dir.relative_to(STORAGE_ROOT.resolve())
    except (ValueError, OSError):
        abort(404)
    transcript_path = meeting_dir / "transcript.txt"
    if not transcript_path.exists():
        abort(404)

    raw = transcript_path.read_text(encoding="utf-8")
    segments = []
    for line in raw.splitlines():
        m = _re.match(r"^(\d+)s\s+-->\s+(.*)", line)
        if m:
            segments.append({"seconds": int(m.group(1)), "text": m.group(2)})

    # Read video title and ID from metadata.json
    video_title = None
    video_id = None
    meta_path = meeting_dir / "metadata.json"
    if meta_path.exists():
        try:
            meta = json.loads(meta_path.read_text())
            video_title = meta.get("video_title") or None
            video_id = meta.get("video_id") or None
        except Exception:
            pass
    # Fall back to using meeting_id as video_id (the directory name IS the video_id now)
    if not video_id:
        video_id = meeting_id

    # Read channel name from channel.json written by rebuild_feed
    channel_name = None
    channel_json = STORAGE_ROOT / channel_slug / "channel.json"
    if channel_json.exists():
        try:
            channel_name = json.loads(channel_json.read_text()).get("channel_name")
        except Exception:
            pass

    return render_template(
        "transcript.html",
        channel_slug=channel_slug,
        meeting_id=meeting_id,
        channel_name=channel_name or channel_slug.replace("_", " "),
        video_title=video_title or meeting_id,
        video_id=video_id,
        segments=segments,
    )


@app.route("/login", methods=["GET", "POST"])
@limiter.limit("10 per minute")
def login():
    if current_user.is_authenticated:
        return redirect(url_for("account"))

    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        remember = bool(request.form.get("remember"))

        user = _find_user_by_email(email)
        if user and check_password_hash(user._data["password_hash"], password):
            if user.is_locked:
                flash("This account has been locked. Contact an administrator.", "error")
            else:
                login_user(user, remember=remember)
                if not user.channel_ids:
                    flash("Welcome! Choose the channels you'd like to follow below.", "success")
                    return redirect(url_for("account"))
                return redirect(_safe_next(request.args.get("next")))
        else:
            flash("Invalid email or password.", "error")

    return render_template("login.html")


@app.route("/register", methods=["GET", "POST"])
@limiter.limit("5 per minute")
def register():
    if current_user.is_authenticated:
        return redirect(url_for("account"))

    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        confirm = request.form.get("confirm_password", "")
        name = request.form.get("name", "").strip() or email.split("@")[0]

        if not email or "@" not in email or "." not in email.split("@")[-1]:
            flash("Please enter a valid email address.", "error")
        elif len(password) < 10:
            flash("Password must be at least 10 characters.", "error")
        elif password != confirm:
            flash("Passwords do not match.", "error")
        elif _find_user_by_email(email):
            flash("An account with that email already exists.", "error")
        else:
            user_uuid = str(uuid.uuid4())
            user_dir = USERS_ROOT / user_uuid
            user_dir.mkdir(parents=True, exist_ok=True)
            data = {
                "name": name,
                "email": email,
                "password_hash": generate_password_hash(password),
                "channels": {},
                "feed_token": str(uuid.uuid4()),
                "created_at": now_utc_iso(),
                "last_accessed": now_utc_iso(),
            }
            (user_dir / "user.json").write_text(json.dumps(data, indent=2))
            _index_add(email, user_uuid)
            login_user(User(user_dir, data))
            _web_ntfy("TubeNews: new user", f"{name} ({email}) registered.")
            flash("Account created! Choose the channels you'd like to follow below.", "success")
            return redirect(url_for("account"))

    return render_template("register.html")


@app.route("/logout", methods=["POST"])
@login_required
def logout():
    logout_user()
    flash("You have been logged out.", "success")
    return redirect(url_for("login"))


@app.route("/feed/<token>.xml")
@app.route("/feed/<token>")
def serve_rss(token: str):
    """Generate and serve a user's RSS feed by secret token — no login required."""
    if not USERS_ROOT.is_dir():
        abort(404)
    for user_json in USERS_ROOT.glob("*/user.json"):
        try:
            data = json.loads(user_json.read_text())
            if data.get("feed_token") == token:
                uid = user_json.parent.name
                xml_bytes = build_user_feed_xml(data, base_url=_base_url(), user_id=uid)
                return Response(xml_bytes, mimetype="application/rss+xml")
        except Exception as exc:
            logger.debug(f"Skipping {user_json.parent.name}: {exc}")
            continue
    abort(404)


@app.route("/feed/<token>.html")
def serve_feed_public(token: str):
    """Render a user's feed page by secret token — no login required."""
    if not USERS_ROOT.is_dir():
        abort(404)
    for user_json in USERS_ROOT.glob("*/user.json"):
        try:
            data = json.loads(user_json.read_text())
            if data.get("feed_token") == token:
                uid = user_json.parent.name
                stories = _get_user_stories(data, uid)
                feed_name = data.get("feed_name") or f"{data['name']}'s TubeNews"
                return render_template("feed.html", stories=stories, feed_name=feed_name,
                                       feed_path=f"/feed/{token}.xml",
                                       body_classes=_prefs_to_classes(data.get("preferences", {})))
        except Exception as exc:
            logger.debug(f"Skipping {user_json.parent.name}: {exc}")
            continue
    abort(404)


@app.route("/feed")
@login_required
def serve_feed():
    """Render the logged-in user's unread (inbox) stories."""
    if not current_user.channel_ids:
        flash("Subscribe to channels to start reading your feed.", "info")
        return redirect(url_for("account"))
    read_set = set(current_user._data.get("read_articles", []))
    all_stories = _get_user_stories(current_user._data, current_user.get_id())
    stories = [s for s in all_stories if s.get("content_hash", "") not in read_set]
    read_count = len(all_stories) - len(stories)
    starred_hashes = set(current_user._data.get("starred_articles", []))
    feed_name = current_user._data.get("feed_name") or f"{current_user.name}'s TubeNews"
    counts = _channel_counts(stories)
    user_bundles = _user_bundles(current_user._data)
    bundle_counts = _bundle_counts(stories, user_bundles)
    active_bundle_slug = request.args.get("bundle", "")
    active_channel_id = "" if active_bundle_slug else request.args.get("channel", "")
    if active_bundle_slug:
        bundle_cids = next((set(b["channel_ids"]) for b in user_bundles if b["slug"] == active_bundle_slug), None)
        if bundle_cids is None:
            abort(404)
        stories = [s for s in stories if s["channel_id"] in bundle_cids]
    elif active_channel_id:
        # Show empty (not 404) when the channel exists but has no unread stories
        if active_channel_id not in current_user.channel_ids:
            abort(404)
        stories = [s for s in stories if s["channel_id"] == active_channel_id]
    return render_template("feed.html", stories=stories, feed_name=feed_name,
                           feed_path=f"/feed/{current_user.feed_token}.xml",
                           read_count=read_count, starred_hashes=starred_hashes,
                           channel_counts=counts, active_channel_id=active_channel_id,
                           bundles=bundle_counts, active_bundle_slug=active_bundle_slug,
                           current_view_url=url_for("serve_feed"))


@app.route("/read")
@login_required
def serve_read():
    """Render the logged-in user's read (archived) stories."""
    if not current_user.channel_ids:
        return redirect(url_for("account"))
    read_set = set(current_user._data.get("read_articles", []))
    all_stories = _get_user_stories(current_user._data, current_user.get_id())
    stories = [s for s in all_stories if s.get("content_hash", "") in read_set]
    query = request.args.get("q", "").strip()[:200]
    if query:
        q = query.lower()
        stories = [s for s in stories if
                   q in s["title"].lower() or
                   q in s["body_html"].lower() or
                   q in s["channel_name"].lower() or
                   q in s["dateline"].lower()]
    starred_hashes = set(current_user._data.get("starred_articles", []))
    feed_name = current_user._data.get("feed_name") or f"{current_user.name}'s TubeNews"
    counts = _channel_counts(stories)
    user_bundles = _user_bundles(current_user._data)
    bundle_counts = _bundle_counts(stories, user_bundles)
    active_bundle_slug = request.args.get("bundle", "")
    active_channel_id = "" if active_bundle_slug else request.args.get("channel", "")
    if active_bundle_slug:
        bundle_cids = next((set(b["channel_ids"]) for b in user_bundles if b["slug"] == active_bundle_slug), None)
        if bundle_cids is None:
            abort(404)
        stories = [s for s in stories if s["channel_id"] in bundle_cids]
    elif active_channel_id:
        if active_channel_id not in current_user.channel_ids:
            abort(404)
        stories = [s for s in stories if s["channel_id"] == active_channel_id]
    return render_template("feed.html", stories=stories, feed_name=feed_name,
                           feed_path=f"/feed/{current_user.feed_token}.xml",
                           is_archive=True, query=query, starred_hashes=starred_hashes,
                           channel_counts=counts, active_channel_id=active_channel_id,
                           bundles=bundle_counts, active_bundle_slug=active_bundle_slug,
                           current_view_url=url_for("serve_read"))


@app.route("/all")
@login_required
def serve_all():
    """Render all of the logged-in user's stories regardless of read status."""
    if not current_user.channel_ids:
        return redirect(url_for("account"))
    stories = _get_user_stories(current_user._data, current_user.get_id())
    query = request.args.get("q", "").strip()[:200]
    if query:
        q = query.lower()
        stories = [s for s in stories if
                   q in s["title"].lower() or
                   q in s["body_html"].lower() or
                   q in s["channel_name"].lower() or
                   q in s["dateline"].lower()]
    starred_hashes = set(current_user._data.get("starred_articles", []))
    feed_name = current_user._data.get("feed_name") or f"{current_user.name}'s TubeNews"
    counts = _channel_counts(stories)
    user_bundles = _user_bundles(current_user._data)
    bundle_counts = _bundle_counts(stories, user_bundles)
    active_bundle_slug = request.args.get("bundle", "")
    active_channel_id = "" if active_bundle_slug else request.args.get("channel", "")
    if active_bundle_slug:
        bundle_cids = next((set(b["channel_ids"]) for b in user_bundles if b["slug"] == active_bundle_slug), None)
        if bundle_cids is None:
            abort(404)
        stories = [s for s in stories if s["channel_id"] in bundle_cids]
    elif active_channel_id:
        if active_channel_id not in current_user.channel_ids:
            abort(404)
        stories = [s for s in stories if s["channel_id"] == active_channel_id]
    current_view = url_for("serve_all", q=query) if query else url_for("serve_all")
    return render_template("feed.html", stories=stories, feed_name=feed_name,
                           feed_path=f"/feed/{current_user.feed_token}.xml",
                           is_all=True, query=query, starred_hashes=starred_hashes,
                           channel_counts=counts, active_channel_id=active_channel_id,
                           bundles=bundle_counts, active_bundle_slug=active_bundle_slug,
                           current_view_url=current_view)


@app.route("/starred")
@login_required
def serve_starred():
    """Render the logged-in user's starred stories."""
    if not current_user.channel_ids:
        return redirect(url_for("account"))
    starred_set = set(current_user._data.get("starred_articles", []))
    all_stories = _get_user_stories(current_user._data, current_user.get_id())
    stories = [s for s in all_stories if s.get("content_hash", "") in starred_set]
    feed_name = current_user._data.get("feed_name") or f"{current_user.name}'s TubeNews"
    counts = _channel_counts(stories)
    user_bundles = _user_bundles(current_user._data)
    bundle_counts = _bundle_counts(stories, user_bundles)
    active_bundle_slug = request.args.get("bundle", "")
    active_channel_id = "" if active_bundle_slug else request.args.get("channel", "")
    if active_bundle_slug:
        bundle_cids = next((set(b["channel_ids"]) for b in user_bundles if b["slug"] == active_bundle_slug), None)
        if bundle_cids is None:
            abort(404)
        stories = [s for s in stories if s["channel_id"] in bundle_cids]
    elif active_channel_id:
        if not any(s["channel_id"] == active_channel_id for s in stories):
            abort(404)
        stories = [s for s in stories if s["channel_id"] == active_channel_id]
    return render_template("feed.html", stories=stories, feed_name=feed_name,
                           feed_path=f"/feed/{current_user.feed_token}.xml",
                           is_starred=True, starred_hashes=starred_set,
                           channel_counts=counts, active_channel_id=active_channel_id,
                           bundles=bundle_counts, active_bundle_slug=active_bundle_slug,
                           current_view_url=url_for("serve_starred"))


@app.route("/channel/<channel_id>")
@login_required
def channel_feed(channel_id: str):
    """Browse all stories for a single configured channel — no time cutoff."""
    channels = _load_channels()
    if not any(ch["channel_id"] == channel_id for ch in channels):
        abort(404)
    user_tz = current_user.preferences.get("timezone", "UTC") if hasattr(current_user, "preferences") else "UTC"
    channel_name, stories = _get_channel_stories(channel_id, user_tz)
    display_name = channel_name or next(
        (ch["channel_name"] for ch in channels if ch["channel_id"] == channel_id), channel_id
    )
    archive_dir = _find_archive_dir_for_channel(channel_id)
    feed_path = f"/content/{archive_dir.name}/rss.xml" if archive_dir else None
    return render_template("feed.html", stories=stories, feed_name=display_name,
                           feed_path=feed_path, channel_id=channel_id)


# ---------------------------------------------------------------------------
# Account self-service routes
# ---------------------------------------------------------------------------


@app.route("/account", methods=["GET", "POST"])
@login_required
def account():
    """User profile: subscriptions, display preferences, account info, and credentials."""
    channels = sorted(_load_channels(), key=lambda ch: ch.get("channel_name", "").lower())

    if request.method == "POST":
        action = request.form.get("action", "")

        if action == "prefs":
            font_size = request.form.get("font_size", "normal")
            if font_size not in ("normal", "large", "larger"):
                font_size = "normal"
            dark_mode = "dark_mode" in request.form
            timezone = request.form.get("timezone", "").strip()
            if timezone and timezone not in pytz.all_timezones:
                flash("Invalid timezone.", "error")
                return redirect(url_for("account"))
            digest_email_enabled = "digest_email_enabled" in request.form
            current_user._data["preferences"] = {"font_size": font_size, "dark_mode": dark_mode,
                                                  "digest_email_enabled": digest_email_enabled}
            if timezone:
                current_user._data["preferences"]["timezone"] = timezone
            current_user._save()
            flash("Display preferences saved.", "success")
            return redirect(url_for("account"))

        if action == "info":
            current_pw = request.form.get("current_password", "")
            if not check_password_hash(current_user._data["password_hash"], current_pw):
                flash("Current password is incorrect.", "error")
                return redirect(url_for("account"))
            new_name = request.form.get("name", "").strip()
            new_email = request.form.get("email", "").strip().lower()
            if not new_name or not new_email or "@" not in new_email:
                flash("Name and a valid email are required.", "error")
                return redirect(url_for("account"))
            if new_email != current_user.email:
                existing = _find_user_by_email(new_email)
                if existing:
                    flash("That email is already in use by another account.", "error")
                    return redirect(url_for("account"))
            old_email = current_user.email
            current_user._data["name"] = new_name
            current_user._data["email"] = new_email
            current_user._save()
            if new_email != old_email:
                _index_remove(old_email)
                _index_add(new_email, current_user.get_id())
            flash("Account info updated.", "success")
            return redirect(url_for("account"))

        # Default: subscription save
        selected = set(request.form.getlist("channel_ids"))
        valid_ids = {ch["channel_id"] for ch in channels}
        new_ids = sorted(selected & valid_ids)
        channels_data = {}
        for ch_id in new_ids:
            raw = request.form.get(f"focus_{ch_id}", "")
            lines = [_sanitize_focus(ln) for ln in raw.splitlines() if ln.strip()][:3]
            lines = [ln for ln in lines if ln]
            channels_data[ch_id] = lines
        current_user._data["channels"] = channels_data
        feed_name = request.form.get("feed_name", "").strip()
        current_user._data["feed_name"] = feed_name
        current_user._data["seen_channel_ids"] = [ch["channel_id"] for ch in channels]
        current_user._save()
        flash("Subscriptions updated.", "success")
        return redirect(url_for("account"))

    # GET: mark all channels as seen so the nav badge clears on this page load.
    current_user._data["seen_channel_ids"] = [ch["channel_id"] for ch in channels]
    current_user._save()

    prefs = current_user._data.get("preferences", {})
    return render_template(
        "account.html",
        channels=channels,
        subscribed=set(current_user.channel_ids),
        channel_focus=current_user._data.get("channels", {}),
        rss_url=_rss_url(current_user.feed_token),
        feed_url=_feed_url(current_user.feed_token) if current_user.channel_ids else None,
        prefs=prefs,
        bundles=_user_bundles(current_user._data),
    )


@app.route("/account/bundles", methods=["POST"])
@login_required
def account_bundles():
    """Save the user's channel bundles from the account page form."""
    bundle_count = int(request.form.get("bundle_count", "0") or "0")
    valid_ids = set(current_user.channel_ids)
    bundles: list[dict] = []
    for i in range(min(bundle_count, 20)):
        name = request.form.get(f"bundle_name_{i}", "").strip()[:100]
        if not name:
            continue  # empty name = delete this bundle
        channel_ids = [cid for cid in request.form.getlist(f"bundle_channels_{i}") if cid in valid_ids]
        bundles.append({"name": name, "channel_ids": channel_ids})
    new_name = request.form.get("new_bundle_name", "").strip()[:100]
    if new_name:
        new_channels = [cid for cid in request.form.getlist("new_bundle_channels") if cid in valid_ids]
        bundles.append({"name": new_name, "channel_ids": new_channels})
    current_user._data["bundles"] = bundles
    current_user._save()
    flash("Bundles saved.", "success")
    return redirect(url_for("account"))


@app.route("/account/password", methods=["POST"])
@login_required
def account_password():
    """Change the logged-in user's own password."""
    current_pw = request.form.get("current_password", "")
    new_pw = request.form.get("new_password", "")
    if not current_pw or not new_pw:
        flash("Both current and new passwords are required.", "error")
        return redirect(url_for("account"))
    if not check_password_hash(current_user._data["password_hash"], current_pw):
        flash("Current password is incorrect.", "error")
        return redirect(url_for("account"))
    if len(new_pw) < 10:
        flash("New password must be at least 10 characters.", "error")
        return redirect(url_for("account"))
    current_user._data["password_hash"] = generate_password_hash(new_pw)
    current_user._save()
    flash("Password updated.", "success")
    return redirect(url_for("account"))


@app.route("/account/rotate-token", methods=["POST"])
@login_required
def account_rotate_token():
    """Issue a new feed token for the logged-in user; invalidates old RSS/feed URLs."""
    current_user._data["feed_token"] = str(uuid.uuid4())
    current_user._save()
    flash("Feed token rotated. Your old RSS and feed URLs are now invalid.", "success")
    return redirect(url_for("account"))


@app.route("/account/delete", methods=["POST"])
@login_required
def account_delete():
    """Delete the logged-in user's own account."""
    current_pw = request.form.get("current_password", "")
    confirm_email = request.form.get("confirm_email", "").strip().lower()
    if not check_password_hash(current_user._data["password_hash"], current_pw):
        flash("Current password is incorrect.", "error")
        return redirect(url_for("account"))
    if confirm_email != current_user.email:
        flash("Email confirmation did not match — account not deleted.", "error")
        return redirect(url_for("account"))
    deleted_email = current_user.email
    uid = current_user.get_id()
    user_dir = USERS_ROOT / uid
    logout_user()
    _index_remove(deleted_email)
    shutil.rmtree(user_dir, ignore_errors=True)
    flash("Your account has been deleted.", "success")
    return redirect(url_for("login"))


@app.route("/account/mark-read", methods=["POST"])
@login_required
def account_mark_read():
    """Mark a story as read (add content_hash to read_articles). Returns JSON."""
    content_hash = request.form.get("content_hash", "").strip()
    if not content_hash:
        return jsonify({"ok": False, "error": "missing content_hash"}), 400
    read_set = set(current_user._data.get("read_articles", []))
    read_set.add(content_hash)
    current_user._data["read_articles"] = sorted(read_set)
    current_user._save()
    return jsonify({"ok": True})


@app.route("/account/mark-unread", methods=["POST"])
@login_required
def account_mark_unread():
    """Mark a story as unread (remove content_hash from read_articles). Returns JSON."""
    content_hash = request.form.get("content_hash", "").strip()
    if not content_hash:
        return jsonify({"ok": False, "error": "missing content_hash"}), 400
    read_set = set(current_user._data.get("read_articles", []))
    read_set.discard(content_hash)
    current_user._data["read_articles"] = sorted(read_set)
    current_user._save()
    return jsonify({"ok": True})


@app.route("/account/mark-all-read", methods=["POST"])
@login_required
def account_mark_all_read():
    """Mark all of the user's current stories as read, then redirect to /feed.

    If a ``bundle_slug`` form field is present, only stories from that bundle's
    channels are marked.  If a ``channel_id`` form field is present, only
    stories from that channel are marked.  The redirect preserves the filter.
    """
    bundle_slug = request.form.get("bundle_slug", "").strip()
    channel_id = request.form.get("channel_id", "").strip()
    all_stories = _get_user_stories(current_user._data, current_user.get_id())
    if bundle_slug:
        bundle_cids = next(
            (set(b["channel_ids"]) for b in _user_bundles(current_user._data) if b["slug"] == bundle_slug), set()
        )
        all_stories = [s for s in all_stories if s.get("channel_id") in bundle_cids]
    elif channel_id:
        all_stories = [s for s in all_stories if s.get("channel_id") == channel_id]
    read_set = set(current_user._data.get("read_articles", []))
    for s in all_stories:
        h = s.get("content_hash", "")
        if h:
            read_set.add(h)
    current_user._data["read_articles"] = sorted(read_set)
    current_user._save()
    if bundle_slug:
        return redirect(url_for("serve_feed") + f"?bundle={bundle_slug}")
    if channel_id:
        return redirect(url_for("serve_feed") + f"?channel={channel_id}")
    return redirect(url_for("serve_feed"))


@app.route("/account/mark-all-unread", methods=["POST"])
@login_required
def account_mark_all_unread():
    """Clear all read articles, then redirect to the inbox.

    If a ``bundle_slug`` form field is present, only stories from that bundle's
    channels are unmarked.  If a ``channel_id`` form field is present, only
    stories from that channel are unmarked.  The redirect preserves the filter.
    """
    bundle_slug = request.form.get("bundle_slug", "").strip()
    channel_id = request.form.get("channel_id", "").strip()
    if bundle_slug:
        bundle_cids = next(
            (set(b["channel_ids"]) for b in _user_bundles(current_user._data) if b["slug"] == bundle_slug), set()
        )
        bundle_hashes = {
            s["content_hash"]
            for s in _get_user_stories(current_user._data, current_user.get_id())
            if s.get("channel_id") in bundle_cids and s.get("content_hash")
        }
        read_set = set(current_user._data.get("read_articles", []))
        read_set -= bundle_hashes
        current_user._data["read_articles"] = sorted(read_set)
    elif channel_id:
        channel_hashes = {
            s["content_hash"]
            for s in _get_user_stories(current_user._data, current_user.get_id())
            if s.get("channel_id") == channel_id and s.get("content_hash")
        }
        read_set = set(current_user._data.get("read_articles", []))
        read_set -= channel_hashes
        current_user._data["read_articles"] = sorted(read_set)
    else:
        current_user._data["read_articles"] = []
    current_user._save()
    if bundle_slug:
        return redirect(url_for("serve_feed") + f"?bundle={bundle_slug}")
    if channel_id:
        return redirect(url_for("serve_feed") + f"?channel={channel_id}")
    return redirect(url_for("serve_feed"))


@app.route("/account/mark-starred", methods=["POST"])
@login_required
def account_mark_starred():
    """Star a story (add content_hash to starred_articles). Returns JSON."""
    content_hash = request.form.get("content_hash", "").strip()
    if not content_hash:
        return jsonify({"ok": False, "error": "missing content_hash"}), 400
    starred_set = set(current_user._data.get("starred_articles", []))
    starred_set.add(content_hash)
    current_user._data["starred_articles"] = sorted(starred_set)
    current_user._save()
    return jsonify({"ok": True})


@app.route("/account/mark-unstarred", methods=["POST"])
@login_required
def account_mark_unstarred():
    """Unstar a story (remove content_hash from starred_articles). Returns JSON."""
    content_hash = request.form.get("content_hash", "").strip()
    if not content_hash:
        return jsonify({"ok": False, "error": "missing content_hash"}), 400
    starred_set = set(current_user._data.get("starred_articles", []))
    starred_set.discard(content_hash)
    current_user._data["starred_articles"] = sorted(starred_set)
    current_user._save()
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# Comment routes
# ---------------------------------------------------------------------------


@app.route("/comments/<channel_slug>/<meeting_id>/<basename>")
@login_required
def get_story_comments(channel_slug: str, meeting_id: str, basename: str):
    """Return the comment list for a story as JSON.

    User names are resolved lazily: if a commenter's account has been deleted,
    their stored user_id will no longer resolve and the name shows as
    'Deleted User' without any modification to the comment file.
    """
    if (not _SAFE_SLUG_RE.match(channel_slug) or
            not _SAFE_SLUG_RE.match(meeting_id) or
            not _SAFE_SLUG_RE.match(basename)):
        abort(400)
    comment_path = STORAGE_ROOT / channel_slug / meeting_id / f"{basename}_comments.json"
    if not comment_path.exists():
        return jsonify([])
    try:
        comments = json.loads(comment_path.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.error(f"Failed to read comment file {comment_path}: {exc}")
        return jsonify([])
    name_cache: dict[str, str] = {}
    my_id = current_user.get_id()
    result = []
    for i, c in enumerate(comments):
        uid = c.get("user_id", "")
        if uid not in name_cache:
            try:
                upath = USERS_ROOT / uid / "user.json"
                name_cache[uid] = (json.loads(upath.read_text()).get("name", "Deleted User")
                                   if upath.exists() else "Deleted User")
            except Exception:
                name_cache[uid] = "Deleted User"
        entry: dict = {
            "idx": i,
            "user_name": name_cache[uid],
            "is_mine": uid == my_id,
            "posted_at": c.get("posted_at", 0.0),
            "body": c.get("body", ""),
        }
        if "edited_at" in c:
            entry["edited_at"] = c["edited_at"]
        result.append(entry)
    return jsonify(result)


@app.route("/comment", methods=["POST"])
@login_required
@limiter.limit("10 per minute")
def post_comment():
    """Append a comment to a story's comment file. Returns JSON."""
    channel_slug = request.form.get("channel_slug", "").strip()
    meeting_id = request.form.get("meeting_id", "").strip()
    filename = request.form.get("filename", "").strip()
    body = request.form.get("body", "").strip()[:2000]
    if (not _SAFE_SLUG_RE.match(channel_slug) or
            not _SAFE_SLUG_RE.match(meeting_id) or
            not _STORY_FILE_RE.match(filename)):
        abort(400)
    if not body:
        return jsonify({"ok": False, "error": "Comment cannot be empty."}), 400
    basename = filename[:-3]
    comment_path = STORAGE_ROOT / channel_slug / meeting_id / f"{basename}_comments.json"
    if not comment_path.parent.is_dir():
        abort(404)
    new_comment = {
        "user_id": current_user.get_id(),
        "user_name": current_user.name,
        "posted_at": now_utc_iso(),
        "body": body,
    }
    try:
        existing = json.loads(comment_path.read_text(encoding="utf-8")) if comment_path.exists() else []
    except Exception as exc:
        logger.error(f"Failed to read comment file {comment_path}: {exc}")
        return jsonify({"ok": False, "error": "Failed to read comments. Please try again."}), 500
    existing.append(new_comment)
    tmp = comment_path.with_suffix(".tmp")
    tmp.write_text(json.dumps(existing, indent=2), encoding="utf-8")
    tmp.rename(comment_path)
    return jsonify({"ok": True, "count": len(existing)})


@app.route("/comment/delete", methods=["POST"])
@login_required
def comment_delete():
    """Delete a comment. Allowed for the comment owner or any admin."""
    channel_slug = request.form.get("channel_slug", "").strip()
    meeting_id   = request.form.get("meeting_id",   "").strip()
    filename     = request.form.get("filename",      "").strip()
    try:
        idx = int(request.form.get("idx", ""))
    except (ValueError, TypeError):
        abort(400)
    if (not _SAFE_SLUG_RE.match(channel_slug) or
            not _SAFE_SLUG_RE.match(meeting_id) or
            not _STORY_FILE_RE.match(filename)):
        abort(400)
    basename = filename[:-3]
    comment_path = STORAGE_ROOT / channel_slug / meeting_id / f"{basename}_comments.json"
    if not comment_path.exists():
        abort(404)
    try:
        comments = json.loads(comment_path.read_text(encoding="utf-8"))
    except Exception:
        abort(500)
    if idx < 0 or idx >= len(comments):
        abort(400)
    my_id = current_user.get_id()
    if comments[idx].get("user_id") != my_id and not current_user.is_admin:
        abort(403)
    del comments[idx]
    tmp = comment_path.with_suffix(".tmp")
    tmp.write_text(json.dumps(comments, indent=2), encoding="utf-8")
    tmp.rename(comment_path)
    return jsonify({"ok": True})


@app.route("/comment/edit", methods=["POST"])
@login_required
def comment_edit():
    """Edit a comment body. Allowed only for the comment owner."""
    channel_slug = request.form.get("channel_slug", "").strip()
    meeting_id   = request.form.get("meeting_id",   "").strip()
    filename     = request.form.get("filename",      "").strip()
    body         = request.form.get("body",          "").strip()[:2000]
    try:
        idx = int(request.form.get("idx", ""))
    except (ValueError, TypeError):
        abort(400)
    if (not _SAFE_SLUG_RE.match(channel_slug) or
            not _SAFE_SLUG_RE.match(meeting_id) or
            not _STORY_FILE_RE.match(filename)):
        abort(400)
    if not body:
        return jsonify({"ok": False, "error": "Comment cannot be empty."}), 400
    basename = filename[:-3]
    comment_path = STORAGE_ROOT / channel_slug / meeting_id / f"{basename}_comments.json"
    if not comment_path.exists():
        abort(404)
    try:
        comments = json.loads(comment_path.read_text(encoding="utf-8"))
    except Exception:
        abort(500)
    if idx < 0 or idx >= len(comments):
        abort(400)
    if comments[idx].get("user_id") != current_user.get_id():
        abort(403)
    comments[idx]["body"] = body
    comments[idx]["edited_at"] = now_utc_iso()
    tmp = comment_path.with_suffix(".tmp")
    tmp.write_text(json.dumps(comments, indent=2), encoding="utf-8")
    tmp.rename(comment_path)
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# Admin routes
# ---------------------------------------------------------------------------


@app.route("/admin")
@login_required
@admin_required
def admin_users():
    channels = _load_channels()
    channel_names = {ch["channel_id"]: ch["channel_name"] for ch in channels}
    return render_template("admin_users.html", users=_all_users(), channel_names=channel_names)


@app.route("/admin/user/<uid>")
@login_required
@admin_required
def admin_user(uid: str):
    user = _find_user_by_id(uid)
    if not user:
        abort(404)
    channels = sorted(_load_channels(), key=lambda ch: ch.get("channel_name", "").lower())
    # Clear the unseen-channel nav badge when an admin views their own profile,
    # since the subscriptions section shows all configured channels.
    if user.get_id() == current_user.get_id():
        user._data["seen_channel_ids"] = [ch["channel_id"] for ch in channels]
        user._save()
    return render_template(
        "admin_user.html",
        u=user,
        channels=channels,
        subscribed=set(user.channel_ids),
        rss_url=_rss_url(user.feed_token),
        feed_url=_feed_url(user.feed_token),
    )


@app.route("/admin/user/<uid>/info", methods=["POST"])
@login_required
@admin_required
def admin_user_info(uid: str):
    user = _find_user_by_id(uid)
    if not user:
        abort(404)
    new_name = request.form.get("name", "").strip()
    new_email = request.form.get("email", "").strip().lower()
    if not new_name or not new_email or "@" not in new_email:
        flash("Name and a valid email are required.", "error")
        return redirect(url_for("admin_user", uid=uid))
    # Check email uniqueness (ignore if unchanged)
    if new_email != user.email:
        existing = _find_user_by_email(new_email)
        if existing:
            flash("That email is already in use by another account.", "error")
            return redirect(url_for("admin_user", uid=uid))
    old_email = user.email
    user._data["name"] = new_name
    user._data["email"] = new_email
    user._data["feed_name"] = request.form.get("feed_name", "").strip()
    user._save()
    if new_email != old_email:
        _index_remove(old_email)
        _index_add(new_email, uid)
    flash("User info updated.", "success")
    return redirect(url_for("admin_user", uid=uid))


@app.route("/admin/user/<uid>/subscriptions", methods=["POST"])
@login_required
@admin_required
def admin_user_subscriptions(uid: str):
    user = _find_user_by_id(uid)
    if not user:
        abort(404)
    channels = _load_channels()
    valid_ids = {ch["channel_id"] for ch in channels}
    new_ids = sorted(set(request.form.getlist("channel_ids")) & valid_ids)
    channels_data = {}
    for ch_id in new_ids:
        raw = request.form.get(f"focus_{ch_id}", "")
        lines = [_sanitize_focus(ln) for ln in raw.splitlines() if ln.strip()][:3]
        lines = [ln for ln in lines if ln]
        channels_data[ch_id] = lines
    user._data["channels"] = channels_data
    user._save()
    flash("Subscriptions updated.", "success")
    return redirect(url_for("admin_user", uid=uid))


@app.route("/admin/user/<uid>/password", methods=["POST"])
@login_required
@admin_required
def admin_user_password(uid: str):
    user = _find_user_by_id(uid)
    if not user:
        abort(404)
    new_pw = request.form.get("new_password", "")
    if len(new_pw) < 10:
        flash("Password must be at least 10 characters.", "error")
        return redirect(url_for("admin_user", uid=uid))
    user._data["password_hash"] = generate_password_hash(new_pw)
    user._save()
    flash("Password updated.", "success")
    return redirect(url_for("admin_user", uid=uid))


@app.route("/admin/user/<uid>/prefs", methods=["POST"])
@login_required
@admin_required
def admin_user_prefs(uid: str):
    user = _find_user_by_id(uid)
    if not user:
        abort(404)
    font_size = request.form.get("font_size", "normal")
    if font_size not in ("normal", "large", "larger"):
        font_size = "normal"
    dark_mode = "dark_mode" in request.form
    timezone = request.form.get("timezone", "").strip()
    if timezone and timezone not in pytz.all_timezones:
        flash("Invalid timezone.", "error")
        return redirect(url_for("admin_user", uid=uid))
    user._data["preferences"] = {"font_size": font_size, "dark_mode": dark_mode}
    if timezone:
        user._data["preferences"]["timezone"] = timezone
    user._save()
    flash("Display preferences updated.", "success")
    return redirect(url_for("admin_user", uid=uid))


@app.route("/admin/user/<uid>/lock", methods=["POST"])
@login_required
@admin_required
def admin_user_lock(uid: str):
    user = _find_user_by_id(uid)
    if not user:
        abort(404)
    if user.email == current_user.email:
        flash("You cannot lock your own account.", "error")
        return redirect(url_for("admin_user", uid=uid))
    user._data["locked"] = not user.is_locked
    user._save()
    flash(f"Account {'locked' if user._data['locked'] else 'unlocked'}.", "success")
    return redirect(url_for("admin_user", uid=uid))


@app.route("/admin/user/<uid>/promote", methods=["POST"])
@login_required
@admin_required
def admin_user_promote(uid: str):
    user = _find_user_by_id(uid)
    if not user:
        abort(404)
    if user.email == current_user.email:
        flash("You cannot change your own admin status.", "error")
        return redirect(url_for("admin_user", uid=uid))
    cfg = _load_config()
    admin_users = [e.strip().lower() for e in cfg.get("admin_users", [])]
    if user.email in admin_users:
        admin_users.remove(user.email)
        flash(f"Admin access revoked for {user.email}.", "success")
    else:
        admin_users.append(user.email)
        flash(f"{user.email} is now an admin.", "success")
    cfg["admin_users"] = admin_users
    tmp = CONFIG_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(cfg, indent=2))
    tmp.replace(CONFIG_FILE)
    return redirect(url_for("admin_user", uid=uid))


@app.route("/admin/user/<uid>/rotate-token", methods=["POST"])
@login_required
@admin_required
def admin_rotate_token(uid: str):
    user = _find_user_by_id(uid)
    if not user:
        abort(404)
    user._data["feed_token"] = str(uuid.uuid4())
    user._save()
    flash("Feed token rotated. The old RSS URL is now invalid.", "success")
    return redirect(url_for("admin_user", uid=uid))


@app.route("/admin/user/<uid>/delete", methods=["POST"])
@login_required
@admin_required
def admin_user_delete(uid: str):
    user = _find_user_by_id(uid)
    if not user:
        abort(404)
    if user.email == current_user.email:
        flash("You cannot delete your own account.", "error")
        return redirect(url_for("admin_user", uid=uid))
    confirm = request.form.get("confirm_email", "").strip().lower()
    if confirm != user.email:
        flash("Email confirmation did not match — account not deleted.", "error")
        return redirect(url_for("admin_user", uid=uid))
    deleted_email = user.email
    _index_remove(deleted_email)
    shutil.rmtree(user._dir, ignore_errors=True)
    _web_ntfy("TubeNews: user deleted", f"{current_user.email} deleted account for {deleted_email}.")
    flash(f"Account for {deleted_email} deleted.", "success")
    return redirect(url_for("admin_users"))


# ---------------------------------------------------------------------------
# Admin feed routes
# ---------------------------------------------------------------------------


@app.route("/admin/users/add", methods=["POST"])
@login_required
@admin_required
def admin_user_add():
    name = request.form.get("name", "").strip()
    email = request.form.get("email", "").strip().lower()
    password = request.form.get("password", "")

    if not name:
        flash("Name is required.", "error")
    elif not email or "@" not in email or "." not in email.split("@")[-1]:
        flash("Please enter a valid email address.", "error")
    elif len(password) < 10:
        flash("Password must be at least 10 characters.", "error")
    elif _find_user_by_email(email):
        flash(f"An account with {email} already exists.", "error")
    else:
        user_uuid = str(uuid.uuid4())
        user_dir = USERS_ROOT / user_uuid
        user_dir.mkdir(parents=True, exist_ok=True)
        data = {
            "name": name,
            "email": email,
            "password_hash": generate_password_hash(password),
            "channels": {},
            "feed_token": str(uuid.uuid4()),
            "created_at": now_utc_iso(),
            "last_accessed": now_utc_iso(),
        }
        (user_dir / "user.json").write_text(json.dumps(data, indent=2))
        _index_add(email, user_uuid)
        flash(f"Account created for {name} ({email}).", "success")

    return redirect(url_for("admin_users"))


@app.route("/admin/runs")
@login_required
@admin_required
def admin_runs():
    run_log_path = STATE_ROOT / "run_logs" / "run_log.json"
    try:
        runs = json.loads(run_log_path.read_text()) if run_log_path.exists() else []
    except Exception:
        runs = []
    starting = request.args.get("starting") == "1"
    is_running = _is_running() or starting
    # Determine which historical runs have a log file available.
    run_logs_dir = STATE_ROOT / "run_logs"
    for run in runs:
        pid = run.get("pid")
        run["has_log"] = bool(pid and (run_logs_dir / f"run-{pid}.log").exists())
    # Pass current running PID so the template can link to the live log.
    current_run_pid = None
    if is_running and not starting:
        try:
            current_run_pid = int(LOCK_FILE.read_text().strip())
        except Exception:
            pass
    return render_template(
        "admin_runs.html",
        runs=list(reversed(runs)),
        channel_stats=_archive_channel_stats(),
        is_running=is_running,
        starting=starting,
        supadata=_get_supadata_balance(),
        current_run_pid=current_run_pid,
    )


@app.route("/admin/run-now", methods=["POST"])
@login_required
@admin_required
def admin_run_now():
    if _is_running():
        flash("TubeNews is already running.", "info")
        return redirect(url_for("admin_runs"))
    cmd = [sys.executable, str(TUBENEWS_PY)]
    if request.form.get("debug"):
        cmd.append("--debug")
    # TubeNews.py writes its own run-<pid>.log via a FileHandler added after
    # lock acquisition, so no stdout/stderr redirect is needed here.
    subprocess.Popen(cmd, start_new_session=True)  # fire-and-forget; start_new_session detaches the process
    _web_ntfy("TubeNews: run started", f"Manual run triggered by {current_user.email}.")
    flash("TubeNews run started.", "success")
    return redirect(url_for("admin_runs") + "?starting=1")


@app.route("/admin/run-log/<int:pid>")
@login_required
@admin_required
def admin_run_log(pid: int):
    log_path = STATE_ROOT / "run_logs" / f"run-{pid}.log"
    content = log_path.read_text(encoding="utf-8") if log_path.exists() else ""
    # Show the live indicator only when this specific PID is the running process.
    try:
        running_pid = int(LOCK_FILE.read_text().strip()) if LOCK_FILE.exists() else None
    except Exception:
        running_pid = None
    is_running = running_pid == pid
    return render_template("admin_run_log.html", content=content, is_running=is_running, pid=pid)


def _get_supadata_balance() -> dict | None:
    """Read cached Supadata credit usage from ``state/supadata_balance.json``.

    The file is written by ``TubeNews._cache_supadata_balance()`` at the end of
    each scraper run, so the web UI never blocks on a live API call.
    Returns ``None`` if the file does not exist or cannot be parsed.
    """
    balance_path = STATE_ROOT / "supadata_balance.json"
    if not balance_path.exists():
        return None
    try:
        return json.loads(balance_path.read_text())
    except Exception:
        return None


@app.route("/admin/story/delete", methods=["POST"])
@login_required
@admin_required
def admin_story_delete():
    """Delete a single story .md file and rebuild the affected feeds."""
    channel_slug = request.form.get("channel_slug", "").strip()
    meeting_id   = request.form.get("meeting_id",   "").strip()
    filename     = request.form.get("filename",      "").strip()

    if not channel_slug or not meeting_id or not filename:
        abort(400)
    if not filename.endswith(".md") or not filename[0].isdigit():
        abort(400)

    # Path traversal guard — resolved path must stay inside STORAGE_ROOT.
    try:
        story_path = (STORAGE_ROOT / channel_slug / meeting_id / filename).resolve()
        story_path.relative_to(STORAGE_ROOT.resolve())
    except ValueError:
        abort(400)

    if not story_path.exists():
        abort(404)

    try:
        story_title = parse_story_file(story_path).get("title", filename)
    except Exception:
        story_title = filename

    story_path.unlink()

    # Rebuild the per-channel feed and the aggregate feed.
    channel_dir  = STORAGE_ROOT / channel_slug
    channels_cfg = _load_channels()
    channel_info = _channel_info_for_dir(channel_dir)
    if channel_info:
        feed_cfg = next(
            (ch for ch in channels_cfg if ch["channel_id"] == channel_info.get("channel_id")),
            None,
        )
        if feed_cfg:
            try:
                rebuild_feed(channel_dir, feed_cfg)
            except Exception:
                pass
    try:
        rebuild_aggregate_feed(base_url=_base_url())
    except Exception:
        pass

    flash(f'Story deleted: \u201c{story_title}\u201d', "info")
    return redirect(url_for("admin_all_stories"))


@app.route("/admin/comment/delete", methods=["POST"])
@login_required
@admin_required
def admin_comment_delete():
    """Delete a single comment by index from a story's comment file. Returns JSON."""
    channel_slug = request.form.get("channel_slug", "").strip()
    meeting_id   = request.form.get("meeting_id",   "").strip()
    filename     = request.form.get("filename",      "").strip()
    try:
        idx = int(request.form.get("idx", ""))
    except (ValueError, TypeError):
        abort(400)
    if (not _SAFE_SLUG_RE.match(channel_slug) or
            not _SAFE_SLUG_RE.match(meeting_id) or
            not _STORY_FILE_RE.match(filename)):
        abort(400)
    basename = filename[:-3]
    comment_path = STORAGE_ROOT / channel_slug / meeting_id / f"{basename}_comments.json"
    if not comment_path.exists():
        abort(404)
    try:
        comments = json.loads(comment_path.read_text(encoding="utf-8"))
    except Exception:
        abort(500)
    if idx < 0 or idx >= len(comments):
        abort(400)
    del comments[idx]
    tmp = comment_path.with_suffix(".tmp")
    tmp.write_text(json.dumps(comments, indent=2), encoding="utf-8")
    tmp.rename(comment_path)
    return jsonify({"ok": True})


@app.route("/admin/feeds")
@login_required
@admin_required
def admin_feeds():
    channels = sorted(_load_channels(), key=lambda ch: ch.get("channel_name", "").lower())
    balance = _get_supadata_balance()
    return render_template("admin_feeds.html", channels=channels, supadata=balance)


@app.route("/admin/feed")
@login_required
@admin_required
def admin_all_stories():
    """Browse all stories from all channels — the feed counterpart to archive/rss.xml."""
    stories = []
    if STORAGE_ROOT.is_dir():
        for channel_dir in STORAGE_ROOT.iterdir():
            if not channel_dir.is_dir() or channel_dir.name == "users" or channel_dir.name.startswith("_"):
                continue
            channel_info = _channel_info_for_dir(channel_dir)
            if not channel_info:
                continue
            channel_name = channel_info.get("channel_name", channel_dir.name.replace("_", " "))
            for meeting_dir in channel_dir.iterdir():
                if not meeting_dir.is_dir():
                    continue
                meta_path = meeting_dir / "metadata.json"
                if not meta_path.exists():
                    continue
                try:
                    meta = json.loads(meta_path.read_text())
                    if meta.get("status") != "processed":
                        continue
                    for story_file in meeting_dir.glob("[0-9]*.md"):
                        s = parse_story_file(story_file)
                        vid = meta["video_id"]
                        vt = meta.get("video_title", "")
                        stories.append({
                            "title": s["title"],
                            "dateline": s["dateline"],
                            "body_html": s["body_html"],
                            "start_seconds": s["start_seconds"],
                            "video_id": vid,
                            "video_title": vt if vt != vid else "",
                            "channel_name": channel_name,
                            "channel_slug": channel_dir.name,
                            "meeting_id": meeting_dir.name,
                            "story_filename": story_file.name,
                            "processed_at": _get_timestamp_as_float(meta.get("processed_at")),
                            "published": s.get("published", ""),
                        })
                except Exception as exc:
                    logger.debug(f"Skipping {meeting_dir}: {exc}")
                    continue
    stories.sort(key=lambda s: s["processed_at"], reverse=True)
    return render_template("feed.html", stories=stories, feed_name="All Channels",
                           feed_path="/content/rss.xml")


@app.route("/admin/feeds/add", methods=["GET", "POST"])
@login_required
@admin_required
def admin_feed_add():
    if request.method == "POST":
        channel_id = request.form.get("channel_id", "").strip()
        channel_name = request.form.get("channel_name", "").strip()
        focus = _sanitize_focus(request.form.get("focus", ""))
        if not channel_id or not channel_name:
            flash("Channel ID and name are required.", "error")
        elif not channel_id.startswith("UC"):
            flash("Channel ID must start with 'UC'.", "error")
        else:
            channels = _load_channels()
            if any(ch["channel_id"] == channel_id for ch in channels):
                flash("A feed with that channel ID already exists.", "error")
            else:
                channels.append({
                    "channel_id": channel_id, "channel_name": channel_name,
                    "focus": focus, "added_at": now_utc_iso(),
                })
                _save_channels(channels)
                config = _load_config()
                if _wsb_subscribe(channel_id, config):
                    flash(f"Feed '{channel_name}' added and subscribed to WebSub.", "success")
                else:
                    flash(f"Feed '{channel_name}' added. (WebSub not configured — skipped.)", "success")
                return redirect(url_for("admin_feeds"))
    return render_template("admin_feed.html", feed=None, channel_id=None)


@app.route("/admin/feeds/<channel_id>/edit", methods=["GET", "POST"])
@login_required
@admin_required
def admin_feed_edit(channel_id: str):
    channels = _load_channels()
    idx = next((i for i, ch in enumerate(channels) if ch["channel_id"] == channel_id), None)
    if idx is None:
        abort(404)
    feed = channels[idx]
    if request.method == "POST":
        new_channel_id = request.form.get("channel_id", "").strip()
        channel_name = request.form.get("channel_name", "").strip()
        focus = _sanitize_focus(request.form.get("focus", ""))
        if not new_channel_id or not channel_name:
            flash("Channel ID and name are required.", "error")
        elif not new_channel_id.startswith("UC"):
            flash("Channel ID must start with 'UC'.", "error")
        else:
            if any(ch["channel_id"] == new_channel_id and i != idx for i, ch in enumerate(channels)):
                flash("Another feed already uses that channel ID.", "error")
            else:
                new_slug = slugify(channel_name)
                rename_error = None
                old_dir = _find_archive_dir_for_channel(channel_id)
                if old_dir is not None and old_dir.name != new_slug:
                    new_dir = STORAGE_ROOT / new_slug
                    if old_dir.exists():
                        # Check if target already exists (rename() may not fail on some systems)
                        if new_dir.exists():
                            rename_error = (
                                f"Archive directory '{new_slug}' already exists — "
                                "rename the existing directory manually before saving."
                            )
                        else:
                            try:
                                old_dir.rename(new_dir)
                            except FileExistsError:
                                rename_error = (
                                    f"Archive directory '{new_slug}' already exists — "
                                    "rename the existing directory manually before saving."
                                )
                            except OSError as exc:
                                rename_error = f"Could not rename archive directory: {exc}"
                elif old_dir is None:
                    # Channel has no channel.json yet; check whether new_slug collides
                    # with an existing directory that belongs to a different channel.
                    candidate = STORAGE_ROOT / new_slug
                    if candidate.is_dir():
                        cj = candidate / "channel.json"
                        if cj.exists():
                            try:
                                existing = json.loads(cj.read_text())
                                if existing.get("channel_id") != channel_id:
                                    rename_error = (
                                        f"Archive directory '{new_slug}' already belongs to "
                                        f"another channel — choose a different channel name."
                                    )
                            except Exception:
                                pass  # corrupt channel.json; safe to overwrite
                if rename_error:
                    flash(rename_error, "error")
                else:
                    # Update channel.json in the (possibly renamed) archive dir.
                    # Guard: only write if the directory either has no channel.json
                    # or the existing channel.json already belongs to this channel.
                    archive_dir = STORAGE_ROOT / new_slug
                    if archive_dir.is_dir():
                        try:
                            existing_cj = archive_dir / "channel.json"
                            safe_to_write = True
                            if existing_cj.exists():
                                try:
                                    existing = json.loads(existing_cj.read_text())
                                    if existing.get("channel_id") not in (channel_id, new_channel_id):
                                        safe_to_write = False
                                except Exception:
                                    pass  # corrupt; overwrite is fine
                            if safe_to_write:
                                existing_cj.write_text(
                                    json.dumps({"channel_id": new_channel_id, "channel_name": channel_name})
                                )
                        except OSError:
                            pass  # non-fatal; next rebuild_feed will overwrite it
                    channels[idx] = {"channel_id": new_channel_id, "channel_name": channel_name, "focus": focus}
                    _save_channels(channels)
                    # If channel_id changed, update WebSub subscription
                    if new_channel_id != channel_id:
                        config = _load_config()
                        _wsb_unsubscribe(channel_id, config)  # unsubscribe from old channel
                        _wsb_subscribe(new_channel_id, config)  # subscribe to new channel
                    flash(f"Feed '{channel_name}' updated.", "success")
                    return redirect(url_for("admin_feeds"))
        feed = {"channel_id": new_channel_id, "channel_name": channel_name, "focus": focus}
        channel_id = new_channel_id
    return render_template("admin_feed.html", feed=feed, channel_id=channel_id)


@app.route("/admin/feeds/<channel_id>/delete", methods=["POST"])
@login_required
@admin_required
def admin_feed_delete(channel_id: str):
    channels = _load_channels()
    idx = next((i for i, ch in enumerate(channels) if ch["channel_id"] == channel_id), None)
    if idx is None:
        abort(404)
    removed = channels.pop(idx)
    config = _load_config()
    _wsb_unsubscribe(removed["channel_id"], config)
    _save_channels(channels)
    flash(f"Feed '{removed['channel_name']}' removed.", "success")
    return redirect(url_for("admin_feeds"))


# ---------------------------------------------------------------------------
# Error handlers
# ---------------------------------------------------------------------------


@app.errorhandler(403)
def forbidden(e):
    return render_template("error.html", code=403, message="You don't have permission to access this page."), 403


@app.errorhandler(404)
def not_found(e):
    return render_template("error.html", code=404, message="Page not found."), 404


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    app.run(host="0.0.0.0", debug=False, port=_port)
