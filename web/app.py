"""TubeNews web UI — account management and feed subscription.

Start the server (always use gunicorn — never python web/app.py):
    ./serve.sh

With HTTPS (behind nginx/Caddy):
    TUBENEWS_HTTPS=true ./serve.sh

The secret key is read from the "tubenews_key" field in TubeNews.json.
Generate one with: python -c 'import secrets; print(secrets.token_hex(32))'
"""

import json
import logging
import os
import subprocess
import sys
import time
import uuid

from datetime import datetime, timezone
from functools import wraps
from pathlib import Path
from typing import TypedDict

from flask import (
    Flask,
    Response,
    abort,
    flash,
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
from werkzeug.security import check_password_hash, generate_password_hash

# ---------------------------------------------------------------------------
# Path setup — import from TubeNews.py in the parent directory
# ---------------------------------------------------------------------------

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

from TubeNews import (  # noqa: E402
    STORAGE_ROOT,
    FeedConfig,
    ParsedStory,
    parse_story_file,
    build_user_feed_xml,
    slugify,
    rebuild_feed,
    rebuild_aggregate_feed,
)

CONFIG_FILE = BASE_DIR / "TubeNews.json"
USERS_ROOT = STORAGE_ROOT / "users"
LOCK_FILE = STORAGE_ROOT / ".tubenews.lock"
TUBENEWS_PY = BASE_DIR / "TubeNews.py"

# ---------------------------------------------------------------------------
# App setup
# ---------------------------------------------------------------------------

app = Flask(__name__)
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
        return self._data.get("channel_ids", [])

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

    def set_channel_ids(self, ids: list[str]) -> None:
        self._data["channel_ids"] = ids
        self._save()

    def _save(self) -> None:
        (self._dir / "user.json").write_text(json.dumps(self._data, indent=2))


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


class StoryDict(TypedDict):
    """Fully-resolved story dict served to Flask templates and the blog/feed builders."""
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


def _save_feeds(feeds: list[FeedConfig]) -> None:
    cfg = _load_config()
    cfg["feeds"] = sorted(feeds, key=lambda ch: ch.get("channel_name", "").lower())
    CONFIG_FILE.write_text(json.dumps(cfg, indent=2))


def _load_channels() -> list[FeedConfig]:
    try:
        return json.loads(CONFIG_FILE.read_text()).get("feeds", [])
    except Exception:
        return []


def _base_url() -> str:
    try:
        return json.loads(CONFIG_FILE.read_text()).get("base_url", "").rstrip("/")
    except Exception:
        return ""


def _feed_url(token: str) -> str:
    base = _base_url()
    if base:
        return f"{base}/feed/{token}.xml"
    return f"/feed/{token}.xml"


def _blog_url(token: str) -> str:
    base = _base_url()
    if base:
        return f"{base}/blog/{token}.html"
    return f"/blog/{token}.html"


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
    else:
        classes = ""
        unseen_count = 0
    return {"body_classes": classes, "unseen_channel_count": unseen_count}


@app.template_filter("format_ts")
def format_ts(ts: int) -> str:
    if not ts:
        return "—"
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")


@app.template_filter("format_datetime")
def format_datetime(ts: int) -> str:
    if not ts:
        return "—"
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


@app.template_filter("focuses_text")
def focuses_text(val) -> str:
    """Render a channel_focus value (str or list[str]) as newline-separated text."""
    if not val:
        return ""
    if isinstance(val, list):
        return "\n".join(val)
    return str(val)


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
    return url_for("serve_blog")


def _channel_info_for_dir(channel_dir: Path, channels_cfg: list[FeedConfig]) -> ChannelInfo | None:
    """Return ``{channel_id, channel_name}`` for *channel_dir*.

    Reads ``channel.json`` when present; falls back to matching the directory
    name against ``slugify(channel_name)`` for each configured channel so that
    old archive directories created before ``channel.json`` was introduced are
    still recognised.
    """
    channel_json = channel_dir / "channel.json"
    if channel_json.exists():
        try:
            return json.loads(channel_json.read_text())
        except Exception:
            pass
    for ch in channels_cfg:
        if slugify(ch["channel_name"]) == channel_dir.name:
            return {"channel_id": ch["channel_id"], "channel_name": ch["channel_name"]}
    return None


def _find_archive_dir_for_channel(channel_id: str) -> Path | None:
    """Return the archive directory whose channel.json matches *channel_id*, or None."""
    if not STORAGE_ROOT.is_dir():
        return None
    for d in STORAGE_ROOT.iterdir():
        if not d.is_dir() or d.name == "users":
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
    channels_cfg = _load_channels()
    for channel_dir in STORAGE_ROOT.iterdir():
        if not channel_dir.is_dir() or channel_dir.name == "users":
            continue
        info = _channel_info_for_dir(channel_dir, channels_cfg)
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
                    last_processed = max(last_processed, meta.get("processed_at", 0))
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


def _get_channel_stories(channel_id: str) -> tuple[str | None, list[StoryDict]]:
    """Return (channel_name, stories) for a single channel, newest-first.

    All processed stories are returned with no time cutoff — this is a full
    archive browse, not a recency-filtered blog view.  Returns (None, []) if
    no matching channel archive is found.
    """
    if not STORAGE_ROOT.is_dir():
        return None, []
    channels_cfg = _load_channels()
    for channel_dir in STORAGE_ROOT.iterdir():
        if not channel_dir.is_dir() or channel_dir.name == "users":
            continue
        channel_info = _channel_info_for_dir(channel_dir, channels_cfg)
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
                                "channel_slug": channel_dir.name, "meeting_id": meeting_dir.name})
            except Exception as exc:
                logger.debug(f"Skipping {meeting_dir}: {exc}")
                continue
        raw.sort(key=lambda e: e["meta"].get("processed_at", 0), reverse=True)
        stories = []
        for entry in raw:
            try:
                s = parse_story_file(entry["file"])
                vid = entry["meta"]["video_id"]
                vt = entry["meta"].get("video_title", "")
                stories.append({
                    "title": s["title"],
                    "dateline": s["dateline"],
                    "body_html": s["body_html"],
                    "start_seconds": s["start_seconds"],
                    "video_id": vid,
                    "video_title": vt if vt != vid else "",
                    "channel_name": entry["channel_name"],
                    "channel_slug": entry.get("channel_slug", ""),
                    "meeting_id": entry.get("meeting_id", ""),
                    "story_filename": entry["file"].name,
                    "processed_at": entry["meta"].get("processed_at", 0),
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
    subscribed = set(user_data.get("channel_ids", []))
    raw: list[dict] = []
    channels_cfg = _load_channels()
    for channel_dir in [d for d in STORAGE_ROOT.iterdir() if d.is_dir() and d.name != "users"]:
        channel_info = _channel_info_for_dir(channel_dir, channels_cfg)
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
    raw.sort(key=lambda e: e["meta"].get("processed_at", 0), reverse=True)
    stories = []
    for entry in raw:
        try:
            s = parse_story_file(entry["file"])
            story_user_ids = s.get("user_ids", [])
            if story_user_ids and user_id not in story_user_ids:
                continue
            vid = entry["meta"]["video_id"]
            vt = entry["meta"].get("video_title", "")
            stories.append({
                "title": s["title"],
                "dateline": s["dateline"],
                "body_html": s["body_html"],
                "start_seconds": s["start_seconds"],
                "video_id": vid,
                "video_title": vt if vt != vid else "",
                "channel_name": entry["channel_name"],
                "channel_slug": entry.get("channel_slug", ""),
                "meeting_id": entry.get("meeting_id", ""),
                "story_filename": entry["file"].name,
                "processed_at": entry["meta"].get("processed_at", 0),
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
        return redirect(url_for("serve_blog"))
    return redirect(url_for("login"))


@app.route("/archive/")
@app.route("/archive/<path:filename>")
def serve_archive(filename=""):
    """Serve static files from the archive directory (feeds, stories, etc.)."""
    if not filename:
        abort(404)
    # Never expose user account data stored under archive/users/
    if filename == "users" or filename.startswith("users/"):
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
    # Fall back to extracting video_id from the directory name
    if not video_id and "_" in meeting_id:
        video_id = meeting_id.split("_", 1)[1]

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
        return redirect(url_for("dashboard"))

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
                return redirect(_safe_next(request.args.get("next")))
        else:
            flash("Invalid email or password.", "error")

    return render_template("login.html")


@app.route("/register", methods=["GET", "POST"])
@limiter.limit("5 per minute")
def register():
    if current_user.is_authenticated:
        return redirect(url_for("dashboard"))

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
                "channel_ids": [],
                "feed_token": str(uuid.uuid4()),
                "created_at": int(datetime.now(timezone.utc).timestamp()),
            }
            (user_dir / "user.json").write_text(json.dumps(data, indent=2))
            _index_add(email, user_uuid)
            login_user(User(user_dir, data))
            _web_ntfy("TubeNews: new user", f"{name} ({email}) registered.")
            flash("Account created. Choose your channels below.", "success")
            return redirect(url_for("dashboard"))

    return render_template("register.html")


@app.route("/dashboard", methods=["GET", "POST"])
@login_required
def dashboard():
    channels = sorted(_load_channels(), key=lambda ch: ch.get("channel_name", "").lower())

    if request.method == "POST":
        if request.form.get("action") == "prefs":
            font_size = request.form.get("font_size", "normal")
            if font_size not in ("normal", "large", "larger"):
                font_size = "normal"
            dark_mode = "dark_mode" in request.form
            current_user._data["preferences"] = {"font_size": font_size, "dark_mode": dark_mode}
            current_user._save()
            flash("Display preferences saved.", "success")
            return redirect(url_for("dashboard"))

        selected = set(request.form.getlist("channel_ids"))
        valid_ids = {ch["channel_id"] for ch in channels}
        new_ids = sorted(selected & valid_ids)
        current_user.set_channel_ids(new_ids)
        blog_name = request.form.get("blog_name", "").strip()
        current_user._data["blog_name"] = blog_name
        channel_focus = {}
        for ch_id in new_ids:
            raw = request.form.get(f"focus_{ch_id}", "")
            lines = [ln.strip() for ln in raw.splitlines() if ln.strip()][:3]
            if lines:
                channel_focus[ch_id] = lines
        current_user._data["channel_focus"] = channel_focus
        current_user._data["seen_channel_ids"] = [ch["channel_id"] for ch in channels]
        current_user._save()
        flash("Subscriptions updated.", "success")
        return redirect(url_for("dashboard"))

    # GET: mark all channels as seen so the nav badge clears on this page load.
    current_user._data["seen_channel_ids"] = [ch["channel_id"] for ch in channels]
    current_user._save()

    prefs = current_user._data.get("preferences", {})
    return render_template(
        "dashboard.html",
        channels=channels,
        subscribed=set(current_user.channel_ids),
        channel_focus=current_user._data.get("channel_focus", {}),
        feed_url=_feed_url(current_user.feed_token),
        blog_url=_blog_url(current_user.feed_token) if current_user.channel_ids else None,
        prefs=prefs,
    )


@app.route("/logout")
@login_required
def logout():
    logout_user()
    flash("You have been logged out.", "success")
    return redirect(url_for("login"))


@app.route("/feed/<token>.xml")
@app.route("/feed/<token>")
def serve_feed(token: str):
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


@app.route("/blog/<token>.html")
@app.route("/blog/<token>")
def serve_blog_public(token: str):
    """Render a user's blog by secret token — no login required."""
    if not USERS_ROOT.is_dir():
        abort(404)
    for user_json in USERS_ROOT.glob("*/user.json"):
        try:
            data = json.loads(user_json.read_text())
            if data.get("feed_token") == token:
                cfg = _load_config()
                uid = user_json.parent.name
                stories = _get_user_stories(data, uid)
                blog_name = data.get("blog_name") or f"{data['name']}'s TubeNews"
                return render_template("blog.html", stories=stories, blog_name=blog_name,
                                       feed_path=f"/feed/{token}.xml",
                                       body_classes=_prefs_to_classes(data.get("preferences", {})))
        except Exception as exc:
            logger.debug(f"Skipping {user_json.parent.name}: {exc}")
            continue
    abort(404)


@app.route("/blog")
@login_required
def serve_blog():
    """Render the logged-in user's blog inside the app template."""
    if not current_user.channel_ids:
        flash("Subscribe to channels to start reading your blog.", "info")
        return redirect(url_for("dashboard"))
    cfg = _load_config()
    stories = _get_user_stories(current_user._data, current_user.get_id())
    blog_name = current_user._data.get("blog_name") or f"{current_user.name}'s TubeNews"
    return render_template("blog.html", stories=stories, blog_name=blog_name,
                           feed_path=f"/feed/{current_user.feed_token}.xml")


@app.route("/channel/<channel_id>")
@login_required
def channel_blog(channel_id: str):
    """Browse all stories for a single configured channel — no time cutoff."""
    channels = _load_channels()
    if not any(ch["channel_id"] == channel_id for ch in channels):
        abort(404)
    channel_name, stories = _get_channel_stories(channel_id)
    display_name = channel_name or next(
        (ch["channel_name"] for ch in channels if ch["channel_id"] == channel_id), channel_id
    )
    archive_dir = _find_archive_dir_for_channel(channel_id)
    feed_path = f"/archive/{archive_dir.name}/rss.xml" if archive_dir else None
    return render_template("blog.html", stories=stories, blog_name=display_name,
                           feed_path=feed_path, channel_id=channel_id)


# ---------------------------------------------------------------------------
# Account self-service routes
# ---------------------------------------------------------------------------


@app.route("/account", methods=["GET", "POST"])
@login_required
def account():
    """Self-service account settings: name, email, feed token URLs."""
    if request.method == "POST":
        new_name = request.form.get("name", "").strip()
        new_email = request.form.get("email", "").strip().lower()
        current_pw = request.form.get("current_password", "")
        if not new_name or not new_email or "@" not in new_email:
            flash("Name and a valid email are required.", "error")
            return redirect(url_for("account"))
        if not check_password_hash(current_user._data["password_hash"], current_pw):
            flash("Current password is incorrect.", "error")
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
    return render_template(
        "account.html",
        feed_url=_feed_url(current_user.feed_token),
        blog_url=_blog_url(current_user.feed_token),
    )


@app.route("/account/password", methods=["POST"])
@login_required
def account_password():
    """Change the logged-in user's own password."""
    current_pw = request.form.get("current_password", "")
    new_pw = request.form.get("new_password", "")
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
    """Issue a new feed token for the logged-in user; invalidates old RSS/blog URLs."""
    current_user._data["feed_token"] = str(uuid.uuid4())
    current_user._save()
    flash("Feed token rotated. Your old RSS and blog URLs are now invalid.", "success")
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
    for f in user_dir.iterdir():
        f.unlink()
    user_dir.rmdir()
    flash("Your account has been deleted.", "success")
    return redirect(url_for("login"))


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
        feed_url=_feed_url(user.feed_token),
        blog_url=_blog_url(user.feed_token),
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
    user._data["blog_name"] = request.form.get("blog_name", "").strip()
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
    user.set_channel_ids(new_ids)
    channel_focus = {}
    for ch_id in new_ids:
        raw = request.form.get(f"focus_{ch_id}", "")
        lines = [ln.strip() for ln in raw.splitlines() if ln.strip()][:3]
        if lines:
            channel_focus[ch_id] = lines
    user._data["channel_focus"] = channel_focus
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
    user._data["preferences"] = {"font_size": font_size, "dark_mode": dark_mode}
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
    CONFIG_FILE.write_text(json.dumps(cfg, indent=2))
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
    for f in user._dir.iterdir():
        f.unlink()
    user._dir.rmdir()
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
            "channel_ids": [],
            "feed_token": str(uuid.uuid4()),
            "created_at": int(datetime.now(timezone.utc).timestamp()),
        }
        (user_dir / "user.json").write_text(json.dumps(data, indent=2))
        _index_add(email, user_uuid)
        flash(f"Account created for {name} ({email}).", "success")

    return redirect(url_for("admin_users"))


@app.route("/admin/runs")
@login_required
@admin_required
def admin_runs():
    run_log_path = STORAGE_ROOT / "run_log.json"
    try:
        runs = json.loads(run_log_path.read_text()) if run_log_path.exists() else []
    except Exception:
        runs = []
    starting = request.args.get("starting") == "1"
    is_running = _is_running() or starting
    return render_template(
        "admin_runs.html",
        runs=list(reversed(runs)),
        channel_stats=_archive_channel_stats(),
        is_running=is_running,
        starting=starting,
        supadata=_get_supadata_balance(),
    )


@app.route("/admin/run-now", methods=["POST"])
@login_required
@admin_required
def admin_run_now():
    if _is_running():
        flash("TubeNews is already running.", "info")
        return redirect(url_for("admin_runs"))
    subprocess.Popen(
        [sys.executable, str(TUBENEWS_PY)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    _web_ntfy("TubeNews: run started", f"Manual run triggered by {current_user.email}.")
    flash("TubeNews run started.", "success")
    return redirect(url_for("admin_runs") + "?starting=1")


def _get_supadata_balance() -> dict | None:
    """Read cached Supadata credit usage from ``archive/supadata_balance.json``.

    The file is written by ``TubeNews._cache_supadata_balance()`` at the end of
    each scraper run, so the web UI never blocks on a live API call.
    Returns ``None`` if the file does not exist or cannot be parsed.
    """
    balance_path = STORAGE_ROOT / "supadata_balance.json"
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
    channel_info = _channel_info_for_dir(channel_dir, channels_cfg)
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
    return redirect(request.referrer or url_for("admin_all_stories"))


@app.route("/admin/feeds")
@login_required
@admin_required
def admin_feeds():
    channels = sorted(_load_channels(), key=lambda ch: ch.get("channel_name", "").lower())
    balance = _get_supadata_balance()
    return render_template("admin_feeds.html", channels=channels, supadata=balance)


@app.route("/admin/blog")
@login_required
@admin_required
def admin_all_stories():
    """Browse all stories from all channels — the blog counterpart to archive/rss.xml."""
    stories = []
    channels_cfg = _load_channels()
    if STORAGE_ROOT.is_dir():
        for channel_dir in STORAGE_ROOT.iterdir():
            if not channel_dir.is_dir() or channel_dir.name == "users":
                continue
            channel_info = _channel_info_for_dir(channel_dir, channels_cfg)
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
                            "processed_at": meta.get("processed_at", 0),
                        })
                except Exception as exc:
                    logger.debug(f"Skipping {meeting_dir}: {exc}")
                    continue
    stories.sort(key=lambda s: s["processed_at"], reverse=True)
    return render_template("blog.html", stories=stories, blog_name="All Channels",
                           feed_path="/archive/rss.xml")


@app.route("/admin/feeds/add", methods=["GET", "POST"])
@login_required
@admin_required
def admin_feed_add():
    if request.method == "POST":
        channel_id = request.form.get("channel_id", "").strip()
        channel_name = request.form.get("channel_name", "").strip()
        focus = request.form.get("focus", "").strip()
        if not channel_id or not channel_name or not focus:
            flash("All fields are required.", "error")
        elif not channel_id.startswith("UC"):
            flash("Channel ID must start with 'UC'.", "error")
        else:
            channels = _load_channels()
            if any(ch["channel_id"] == channel_id for ch in channels):
                flash("A feed with that channel ID already exists.", "error")
            else:
                channels.append({"channel_id": channel_id, "channel_name": channel_name, "focus": focus, "added_at": int(time.time())})
                _save_feeds(channels)
                flash(f"Feed '{channel_name}' added.", "success")
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
        focus = request.form.get("focus", "").strip()
        if not new_channel_id or not channel_name or not focus:
            flash("All fields are required.", "error")
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
                        if new_dir.exists():
                            rename_error = (
                                f"Archive directory '{new_slug}' already exists — "
                                "rename the existing directory manually before saving."
                            )
                        else:
                            try:
                                old_dir.rename(new_dir)
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
                    _save_feeds(channels)
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
    _save_feeds(channels)
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
    app.run(host="0.0.0.0", debug=True, port=_port)
