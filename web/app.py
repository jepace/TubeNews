"""TubeNews web UI — account management and feed subscription.

Run in development:
    python web/app.py

Run in production (behind nginx/Caddy with TLS):
    gunicorn -w 2 'web.app:app'

The secret key is read from the "tubenews_key" field in TubeNews.json.
Generate one with: python -c 'import secrets; print(secrets.token_hex(32))'
"""

import json
import os
import sys
import uuid
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path

from flask import (
    Flask,
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

from TubeNews import STORAGE_ROOT, rebuild_user_blog, rebuild_user_feed  # noqa: E402

CONFIG_FILE = BASE_DIR / "TubeNews.json"
USERS_ROOT = STORAGE_ROOT / "users"

# ---------------------------------------------------------------------------
# App setup
# ---------------------------------------------------------------------------

app = Flask(__name__)

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
            return self.email in [e.strip().lower() for e in cfg.get("admin_emails", [])]
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


def _find_user_by_email(email: str) -> User | None:
    if not USERS_ROOT.is_dir():
        return None
    needle = email.strip().lower()
    for user_json in USERS_ROOT.glob("*/user.json"):
        try:
            data = json.loads(user_json.read_text())
            if data.get("email", "").lower() == needle:
                return User(user_json.parent, data)
        except Exception:
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
        except Exception:
            continue
    return sorted(users, key=lambda u: u.name.lower())


@login_manager.user_loader
def load_user(user_id: str) -> User | None:
    return _find_user_by_id(user_id)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _load_config() -> dict:
    try:
        return json.loads(CONFIG_FILE.read_text())
    except Exception:
        return {}


def _save_feeds(feeds: list[dict]) -> None:
    cfg = _load_config()
    cfg["feeds"] = feeds
    CONFIG_FILE.write_text(json.dumps(cfg, indent=2))


def _load_channels() -> list[dict]:
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
    return url_for("serve_feed", token=token, _external=True).replace(f"/feed/{token}", f"/feed/{token}.xml")

@app.template_filter("format_ts")
def format_ts(ts: int) -> str:
    if not ts:
        return "—"
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")


def admin_required(f):
    """Decorator: 403 unless the logged-in user is an admin."""
    @wraps(f)
    def decorated(*args, **kwargs):
        if not current_user.is_authenticated or not current_user.is_admin:
            abort(403)
        return f(*args, **kwargs)
    return decorated


# ---------------------------------------------------------------------------
# Public routes
# ---------------------------------------------------------------------------


@app.route("/")
def index():
    if current_user.is_authenticated:
        return redirect(url_for("dashboard"))
    return redirect(url_for("login"))


@app.route("/archive/")
@app.route("/archive/<path:filename>")
def serve_archive(filename=""):
    """Serve static files from the archive directory (feeds, stories, etc.)."""
    if not filename:
        abort(404)
    mimetype = "application/rss+xml" if filename.endswith(".xml") else None
    return send_from_directory(STORAGE_ROOT, filename, mimetype=mimetype)


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
                return redirect(request.args.get("next") or url_for("dashboard"))
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
            login_user(User(user_dir, data))
            flash("Account created. Choose your channels below.", "success")
            return redirect(url_for("dashboard"))

    return render_template("register.html")


@app.route("/dashboard", methods=["GET", "POST"])
@login_required
def dashboard():
    channels = _load_channels()

    if request.method == "POST":
        selected = set(request.form.getlist("channel_ids"))
        valid_ids = {ch["channel_id"] for ch in channels}
        current_user.set_channel_ids(sorted(selected & valid_ids))
        cfg = _load_config()
        try:
            rebuild_user_feed(current_user._data, base_url=_base_url())
            rebuild_user_blog(current_user._data, base_url=_base_url(), blog_days=cfg.get("blog_days", 90))
        except Exception as exc:
            flash(f"Subscriptions saved, but feed rebuild failed: {exc}", "error")
        else:
            flash("Subscriptions updated.", "success")
        return redirect(url_for("dashboard"))

    return render_template(
        "dashboard.html",
        channels=channels,
        subscribed=set(current_user.channel_ids),
        feed_url=_feed_url(current_user.feed_token),
        blog_url=url_for("serve_blog") + ".html" if current_user.channel_ids else None,
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
    """Serve a user's RSS feed by secret token — no login required."""
    if not USERS_ROOT.is_dir():
        abort(404)
    for user_json in USERS_ROOT.glob("*/user.json"):
        try:
            data = json.loads(user_json.read_text())
            if data.get("feed_token") == token:
                rss_path = user_json.parent / "rss.xml"
                if rss_path.exists():
                    return send_file(rss_path, mimetype="application/rss+xml")
                abort(404)
        except Exception:
            continue
    abort(404)


@app.route("/blog.html")
@app.route("/blog")
@login_required
def serve_blog():
    """Regenerate and serve the current user's blog page."""
    cfg = _load_config()
    try:
        rebuild_user_blog(current_user._data, base_url=_base_url(), blog_days=cfg.get("blog_days", 90))
    except Exception as exc:
        flash(f"Blog generation failed: {exc}", "error")
        return redirect(url_for("dashboard"))
    from TubeNews import slugify
    blog_path = USERS_ROOT / slugify(current_user._data["name"]) / "index.html"
    if not blog_path.exists():
        flash("No blog content yet — run TubeNews.py to fetch stories first.", "error")
        return redirect(url_for("dashboard"))
    return send_file(blog_path, mimetype="text/html")


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
    channels = _load_channels()
    return render_template(
        "admin_user.html",
        u=user,
        channels=channels,
        subscribed=set(user.channel_ids),
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
    user._data["name"] = new_name
    user._data["email"] = new_email
    user._save()
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
    selected = set(request.form.getlist("channel_ids")) & valid_ids
    user.set_channel_ids(sorted(selected))
    try:
        rebuild_user_feed(user._data, base_url=_base_url())
    except Exception as exc:
        flash(f"Saved, but feed rebuild failed: {exc}", "error")
    else:
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
    for f in user._dir.iterdir():
        f.unlink()
    user._dir.rmdir()
    flash(f"Account for {user.email} deleted.", "success")
    return redirect(url_for("admin_users"))


# ---------------------------------------------------------------------------
# Admin feed routes
# ---------------------------------------------------------------------------


@app.route("/admin/feeds")
@login_required
@admin_required
def admin_feeds():
    channels = sorted(_load_channels(), key=lambda ch: ch.get("channel_name", "").lower())
    return render_template("admin_feeds.html", channels=channels)


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
                channels.append({"channel_id": channel_id, "channel_name": channel_name, "focus": focus})
                _save_feeds(channels)
                flash(f"Feed '{channel_name}' added.", "success")
                return redirect(url_for("admin_feeds"))
    return render_template("admin_feed.html", feed=None, idx=None)


@app.route("/admin/feeds/<int:idx>/edit", methods=["GET", "POST"])
@login_required
@admin_required
def admin_feed_edit(idx: int):
    channels = _load_channels()
    if idx < 0 or idx >= len(channels):
        abort(404)
    feed = channels[idx]
    if request.method == "POST":
        channel_id = request.form.get("channel_id", "").strip()
        channel_name = request.form.get("channel_name", "").strip()
        focus = request.form.get("focus", "").strip()
        if not channel_id or not channel_name or not focus:
            flash("All fields are required.", "error")
        elif not channel_id.startswith("UC"):
            flash("Channel ID must start with 'UC'.", "error")
        else:
            if any(ch["channel_id"] == channel_id and i != idx for i, ch in enumerate(channels)):
                flash("Another feed already uses that channel ID.", "error")
            else:
                channels[idx] = {"channel_id": channel_id, "channel_name": channel_name, "focus": focus}
                _save_feeds(channels)
                flash(f"Feed '{channel_name}' updated.", "success")
                return redirect(url_for("admin_feeds"))
        feed = {"channel_id": channel_id, "channel_name": channel_name, "focus": focus}
    return render_template("admin_feed.html", feed=feed, idx=idx)


@app.route("/admin/feeds/<int:idx>/delete", methods=["POST"])
@login_required
@admin_required
def admin_feed_delete(idx: int):
    channels = _load_channels()
    if idx < 0 or idx >= len(channels):
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
