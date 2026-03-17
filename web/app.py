"""TubeNews web UI — account management and feed subscription.

Run in development:
    export TUBENEWS_SECRET_KEY="$(python -c 'import secrets; print(secrets.token_hex(32))')"
    python web/app.py

Run in production (behind nginx/Caddy with TLS):
    gunicorn -w 2 'web.app:app'
"""

import json
import os
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

from flask import (
    Flask,
    abort,
    flash,
    redirect,
    render_template,
    request,
    send_file,
    session,
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

from TubeNews import STORAGE_ROOT, rebuild_user_feed, slugify  # noqa: E402

CONFIG_FILE = BASE_DIR / "TubeNews.json"
USERS_ROOT = STORAGE_ROOT / "users"

# ---------------------------------------------------------------------------
# App setup
# ---------------------------------------------------------------------------

app = Flask(__name__)

secret_key = os.environ.get("TUBENEWS_SECRET_KEY")
if not secret_key:
    raise RuntimeError(
        "TUBENEWS_SECRET_KEY environment variable is not set. "
        "Generate one with: python -c 'import secrets; print(secrets.token_hex(32))'"
    )
app.config["SECRET_KEY"] = secret_key
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
# Set SECURE=True when running behind HTTPS in production.
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

    # flask-login requires get_id() to return a string
    def get_id(self) -> str:
        return self._dir.name  # the UUID directory name

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

    def set_channel_ids(self, ids: list[str]) -> None:
        self._data["channel_ids"] = ids
        self._save()

    def _save(self) -> None:
        (self._dir / "user.json").write_text(json.dumps(self._data, indent=2))


def _find_user_by_email(email: str) -> User | None:
    """Scan archive/users/*/user.json for a matching email (case-insensitive)."""
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
    """Load a user by their UUID directory name (used by flask-login)."""
    user_json = USERS_ROOT / user_id / "user.json"
    if not user_json.exists():
        return None
    try:
        return User(user_json.parent, json.loads(user_json.read_text()))
    except Exception:
        return None


@login_manager.user_loader
def load_user(user_id: str) -> User | None:
    return _find_user_by_id(user_id)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _load_channels() -> list[dict]:
    """Return the list of feed configs from TubeNews.json."""
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
        return f"{base}/feed/{token}"
    return url_for("serve_feed", token=token, _external=True)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@app.route("/")
def index():
    if current_user.is_authenticated:
        return redirect(url_for("dashboard"))
    return redirect(url_for("login"))


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
            login_user(user, remember=remember)
            next_page = request.args.get("next")
            return redirect(next_page or url_for("dashboard"))

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

        # Basic validation
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
            user = User(user_dir, data)
            login_user(user)
            flash("Account created. Choose your channels below.", "success")
            return redirect(url_for("dashboard"))

    return render_template("register.html")


@app.route("/dashboard", methods=["GET", "POST"])
@login_required
def dashboard():
    channels = _load_channels()

    if request.method == "POST":
        selected = set(request.form.getlist("channel_ids"))
        # Only allow valid channel IDs from the config
        valid_ids = {ch["channel_id"] for ch in channels}
        current_user.set_channel_ids(sorted(selected & valid_ids))
        # Rebuild the RSS feed immediately
        try:
            rebuild_user_feed(current_user._data, base_url=_base_url())
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
    )


@app.route("/logout")
@login_required
def logout():
    logout_user()
    flash("You have been logged out.", "success")
    return redirect(url_for("login"))


@app.route("/feed/<token>")
def serve_feed(token: str):
    """Serve a user's RSS feed by their secret feed token (no login required)."""
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


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    app.run(debug=True, port=5000)
