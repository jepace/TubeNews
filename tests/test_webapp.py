"""Integration tests for the TubeNews Flask web application.

These tests use Flask's test client so every route is exercised end-to-end,
including imports, middleware, and template rendering.  No network calls are
made; all archive data is written to a pytest tmp_path.

Run with:  pytest tests/test_webapp.py -v
"""
import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from werkzeug.security import generate_password_hash

# Set the secret key before importing web.app — the module raises RuntimeError
# at import time if neither tubenews_key nor TUBENEWS_SECRET_KEY is set.
os.environ.setdefault("TUBENEWS_SECRET_KEY", "test-secret-key-32-bytes-xxxxxxxx")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import web.app as webapp
from web.app import app as flask_app

# Disable rate limiting globally for the test session.  Flask-Limiter checks
# this config key on every request, so setting it once at import time is enough.
flask_app.config["RATELIMIT_ENABLED"] = False


# ---------------------------------------------------------------------------
# Archive / user helpers (mirrors patterns in test_tubenews.py)
# ---------------------------------------------------------------------------

def _write_story(meeting_dir: Path, filename: str, title: str,
                 dateline: str = "TESTVILLE — Jan 15, 2026",
                 content: str = "Story body text.", start_seconds: int = 120) -> None:
    (meeting_dir / filename).write_text(
        f"# {title}\n*{dateline}*\n\n{content}\n\n---\n**Segment Start:** {start_seconds}s\n",
        encoding="utf-8",
    )


def _make_meeting(channel_dir: Path, date_prefix: str, video_id: str,
                  title: str, status: str = "processed") -> Path:
    meeting_dir = channel_dir / f"{date_prefix}_{video_id}"
    meeting_dir.mkdir(parents=True, exist_ok=True)
    (meeting_dir / "metadata.json").write_text(json.dumps({
        "video_id": video_id,
        "video_title": title,
        "status": status,
        "processed_at": int(time.time()),
    }))
    return meeting_dir


def _make_channel(archive_root: Path, slug: str, channel_id: str,
                  channel_name: str, story_title: str | None = None) -> Path:
    channel_dir = archive_root / slug
    meeting_dir = _make_meeting(channel_dir, "2026-01-15", "VID12345678", f"{channel_name} Meeting")
    _write_story(meeting_dir, "01_Story.md",
                 story_title or f"Story from {channel_name}",
                 f"TESTVILLE — Jan 15, 2026", "Story body.", 120)
    (channel_dir / "channel.json").write_text(
        json.dumps({"channel_id": channel_id, "channel_name": channel_name})
    )
    return channel_dir


def _make_user(users_root: Path, name: str, email: str, channel_ids: list[str],
               token: str | None = None, password: str = "testpassword123") -> dict:
    token = token or str(uuid.uuid4())
    user_dir = users_root / str(uuid.uuid4())
    user_dir.mkdir(parents=True, exist_ok=True)
    data = {
        "name": name,
        "email": email,
        "password_hash": generate_password_hash(password),
        "channel_ids": channel_ids,
        "feed_token": token,
        "created_at": int(time.time()),
    }
    (user_dir / "user.json").write_text(json.dumps(data))
    return data


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _patch_config(tmp_path, monkeypatch):
    """Point CONFIG_FILE at a temp file so tests never read the real TubeNews.json."""
    cfg_path = tmp_path / "TubeNews.json"
    cfg_path.write_text(json.dumps({
        "tubenews_key": "test-secret-key-32-bytes-xxxxxxxx",
        "gemini_api_key": "test",
        "supadata_api_key": "test",
        "feeds": [
            {"channel_id": "UC_ALPHA_ID", "channel_name": "Alpha City Council", "focus": "housing"},
            {"channel_id": "UC_BETA__ID", "channel_name": "Beta City Council",  "focus": "zoning"},
        ],
    }))
    monkeypatch.setattr(webapp, "CONFIG_FILE", cfg_path)


@pytest.fixture
def archive(tmp_path, monkeypatch):
    """Temp archive with two channels; patches every STORAGE_ROOT reference."""
    import TubeNews
    monkeypatch.setattr(webapp,    "STORAGE_ROOT", tmp_path)
    monkeypatch.setattr(webapp,    "USERS_ROOT",   tmp_path / "users")
    monkeypatch.setattr(TubeNews,  "STORAGE_ROOT", tmp_path)

    _make_channel(tmp_path, "alpha_city", "UC_ALPHA_ID", "Alpha City Council",
                  story_title="Alpha Council Approves Budget")
    _make_channel(tmp_path, "beta_city",  "UC_BETA__ID", "Beta City Council",
                  story_title="Beta Council Discusses Zoning")

    (tmp_path / "users").mkdir()
    return tmp_path


@pytest.fixture
def registered_user(archive):
    """A user subscribed to Alpha only, with a fixed known feed token."""
    return _make_user(
        archive / "users",
        name="Test User",
        email="test@example.com",
        channel_ids=["UC_ALPHA_ID"],
        token="known-test-feed-token-abc123",
    )


@pytest.fixture
def client():
    flask_app.config["TESTING"] = True
    flask_app.config["WTF_CSRF_ENABLED"] = False
    webapp.limiter.reset()   # clear in-memory rate-limit counters between tests
    with flask_app.test_client() as c:
        yield c


@pytest.fixture
def logged_in_client(client, registered_user):
    """A test client already authenticated as registered_user."""
    client.post("/login", data={
        "email": "test@example.com",
        "password": "testpassword123",
    })
    return client


# ---------------------------------------------------------------------------
# RSS feed route — the fundamental feature of the product
# ---------------------------------------------------------------------------

def test_serve_feed_xml_extension_returns_200(client, registered_user):
    """GET /feed/<token>.xml must return HTTP 200 for a valid token."""
    r = client.get(f"/feed/{registered_user['feed_token']}.xml")
    assert r.status_code == 200


def test_serve_feed_content_type_is_rss(client, registered_user):
    """RSS feed must be served as application/rss+xml, not text/html or application/octet-stream."""
    r = client.get(f"/feed/{registered_user['feed_token']}.xml")
    assert "application/rss+xml" in r.content_type


def test_serve_feed_without_extension_returns_200(client, registered_user):
    """GET /feed/<token> (no .xml suffix) must also work for backwards compatibility."""
    r = client.get(f"/feed/{registered_user['feed_token']}")
    assert r.status_code == 200


def test_serve_feed_invalid_token_returns_404(client, archive):
    """Unknown feed token must return 404, not a 500 crash."""
    r = client.get("/feed/no-such-token-at-all.xml")
    assert r.status_code == 404


def test_serve_feed_is_valid_rss_envelope(client, registered_user):
    """Feed body must contain opening and closing <rss> tags."""
    r = client.get(f"/feed/{registered_user['feed_token']}.xml")
    body = r.data.decode()
    assert "<rss" in body
    assert "</rss>" in body


def test_serve_feed_includes_subscribed_stories(client, registered_user):
    """Stories from a subscribed channel must appear in the RSS feed."""
    r = client.get(f"/feed/{registered_user['feed_token']}.xml")
    assert b"Alpha Council Approves Budget" in r.data


def test_serve_feed_excludes_unsubscribed_stories(client, registered_user):
    """Stories from a channel the user is not subscribed to must not appear."""
    r = client.get(f"/feed/{registered_user['feed_token']}.xml")
    assert b"Beta Council Discusses Zoning" not in r.data


def test_serve_feed_includes_youtube_link(client, registered_user):
    """Each story entry must link back to the source YouTube video."""
    r = client.get(f"/feed/{registered_user['feed_token']}.xml")
    assert b"youtu.be/" in r.data


def test_serve_feed_includes_timestamp_param(client, registered_user):
    """YouTube links must include the ?t= timestamp for deep-linking."""
    r = client.get(f"/feed/{registered_user['feed_token']}.xml")
    assert b"?t=" in r.data


# ---------------------------------------------------------------------------
# Public blog route
# ---------------------------------------------------------------------------

def test_serve_blog_public_returns_200(client, registered_user):
    r = client.get(f"/blog/{registered_user['feed_token']}.html")
    assert r.status_code == 200


def test_serve_blog_public_content_type_is_html(client, registered_user):
    r = client.get(f"/blog/{registered_user['feed_token']}.html")
    assert "text/html" in r.content_type


def test_serve_blog_public_invalid_token_returns_404(client, archive):
    r = client.get("/blog/no-such-token.html")
    assert r.status_code == 404


def test_serve_blog_public_includes_subscribed_stories(client, registered_user):
    r = client.get(f"/blog/{registered_user['feed_token']}.html")
    assert b"Alpha Council Approves Budget" in r.data


def test_serve_blog_public_excludes_unsubscribed_stories(client, registered_user):
    r = client.get(f"/blog/{registered_user['feed_token']}.html")
    assert b"Beta Council Discusses Zoning" not in r.data


def test_serve_blog_without_extension_returns_200(client, registered_user):
    r = client.get(f"/blog/{registered_user['feed_token']}")
    assert r.status_code == 200


# ---------------------------------------------------------------------------
# Per-channel blog roll route
# ---------------------------------------------------------------------------

def test_channel_blog_redirects_to_login_when_anonymous(client, archive):
    """Unauthenticated requests to /channel/<id> must redirect to login."""
    r = client.get("/channel/UC_ALPHA_ID")
    assert r.status_code == 302
    assert "/login" in r.headers["Location"]


def test_channel_blog_returns_200_when_logged_in(logged_in_client):
    r = logged_in_client.get("/channel/UC_ALPHA_ID")
    assert r.status_code == 200


def test_channel_blog_shows_channel_stories(logged_in_client):
    r = logged_in_client.get("/channel/UC_ALPHA_ID")
    assert b"Alpha Council Approves Budget" in r.data


def test_channel_blog_does_not_show_other_channel_stories(logged_in_client):
    """The Alpha channel blog roll must not include Beta's stories."""
    r = logged_in_client.get("/channel/UC_ALPHA_ID")
    assert b"Beta Council Discusses Zoning" not in r.data


def test_channel_blog_unknown_channel_returns_404(logged_in_client):
    r = logged_in_client.get("/channel/UC_NOT_IN_CONFIG")
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# Login / auth
# ---------------------------------------------------------------------------

def test_login_valid_credentials_redirects(client, registered_user):
    r = client.post("/login", data={
        "email": "test@example.com",
        "password": "testpassword123",
    }, follow_redirects=False)
    assert r.status_code == 302


def test_login_wrong_password_shows_error(client, registered_user):
    r = client.post("/login", data={
        "email": "test@example.com",
        "password": "wrongpassword!!",
    }, follow_redirects=True)
    assert b"Invalid email or password" in r.data


def test_login_unknown_email_shows_error(client, archive):
    r = client.post("/login", data={
        "email": "nobody@example.com",
        "password": "testpassword123",
    }, follow_redirects=True)
    assert b"Invalid email or password" in r.data


def test_logout_redirects_to_login(logged_in_client):
    r = logged_in_client.get("/logout", follow_redirects=False)
    assert r.status_code == 302


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------

def test_dashboard_requires_login(client, archive):
    r = client.get("/dashboard")
    assert r.status_code == 302
    assert "/login" in r.headers["Location"]


def test_dashboard_shows_channels(logged_in_client):
    r = logged_in_client.get("/dashboard")
    assert b"Alpha City Council" in r.data
    assert b"Beta City Council" in r.data


def test_dashboard_shows_feed_url_when_subscribed(logged_in_client):
    r = logged_in_client.get("/dashboard")
    # The sharing URL section appears when the user has subscriptions.
    assert b"known-test-feed-token-abc123" in r.data


def test_dashboard_subscribe_updates_channels(logged_in_client, archive):
    r = logged_in_client.post("/dashboard", data={
        "channel_ids": ["UC_ALPHA_ID", "UC_BETA__ID"],
    }, follow_redirects=True)
    assert r.status_code == 200
    assert b"Subscriptions updated" in r.data


# ---------------------------------------------------------------------------
# Admin fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def admin_user(archive, monkeypatch):
    """A user whose email is in admin_users; updates the temp config accordingly."""
    cfg_path = webapp.CONFIG_FILE
    cfg = json.loads(cfg_path.read_text())
    cfg["admin_users"] = ["admin@example.com"]
    cfg_path.write_text(json.dumps(cfg))
    # Also patch LOCK_FILE so admin routes never touch the real filesystem.
    monkeypatch.setattr(webapp, "LOCK_FILE", archive / ".tubenews.lock")
    return _make_user(
        archive / "users",
        name="Admin",
        email="admin@example.com",
        channel_ids=[],
    )


@pytest.fixture
def admin_client(client, admin_user):
    """A test client authenticated as the admin user."""
    client.post("/login", data={
        "email": "admin@example.com",
        "password": "testpassword123",
    })
    return client


# ---------------------------------------------------------------------------
# _is_running helper
# ---------------------------------------------------------------------------

def test_is_running_false_when_no_lock_file(tmp_path, monkeypatch):
    """Returns False when no lock file exists."""
    monkeypatch.setattr(webapp, "LOCK_FILE", tmp_path / ".tubenews.lock")
    assert not webapp._is_running()


def test_is_running_true_when_lock_has_live_pid(tmp_path, monkeypatch):
    """Returns True when the lock file contains a currently-running PID."""
    lock = tmp_path / ".tubenews.lock"
    monkeypatch.setattr(webapp, "LOCK_FILE", lock)
    lock.write_text(str(os.getpid()))
    assert webapp._is_running()


def test_is_running_false_when_lock_has_dead_pid(tmp_path, monkeypatch):
    """Returns False (and doesn't raise) when the lock PID is not alive."""
    lock = tmp_path / ".tubenews.lock"
    monkeypatch.setattr(webapp, "LOCK_FILE", lock)
    # Spawn a process, wait for it to finish, then use its (now-dead) PID.
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    dead_pid = proc.pid
    proc.wait()
    lock.write_text(str(dead_pid))
    assert not webapp._is_running()


def test_is_running_false_when_lock_contains_garbage(tmp_path, monkeypatch):
    """Returns False when the lock file has non-numeric content."""
    lock = tmp_path / ".tubenews.lock"
    monkeypatch.setattr(webapp, "LOCK_FILE", lock)
    lock.write_text("not-a-pid")
    assert not webapp._is_running()


# ---------------------------------------------------------------------------
# Admin runs page — Run Now button and status banner
# ---------------------------------------------------------------------------

def test_admin_runs_requires_login(client, archive):
    r = client.get("/admin/runs")
    assert r.status_code == 302
    assert "/login" in r.headers["Location"]


def test_admin_runs_requires_admin(logged_in_client, archive):
    r = logged_in_client.get("/admin/runs")
    assert r.status_code == 403


def test_admin_runs_shows_run_now_button_when_idle(admin_client, archive):
    """When no lock file exists the page must show the Run Now button."""
    r = admin_client.get("/admin/runs")
    assert r.status_code == 200
    assert b"Run Now" in r.data


def test_admin_runs_shows_running_banner_when_locked(admin_client, archive):
    """When the lock file contains our PID the page must show 'Running'."""
    (archive / ".tubenews.lock").write_text(str(os.getpid()))
    r = admin_client.get("/admin/runs")
    assert b"Running" in r.data
    assert b"Run Now" not in r.data


# ---------------------------------------------------------------------------
# POST /admin/run-now
# ---------------------------------------------------------------------------

def test_run_now_requires_login(client, archive):
    r = client.post("/admin/run-now")
    assert r.status_code == 302
    assert "/login" in r.headers["Location"]


def test_run_now_requires_admin(logged_in_client, archive):
    r = logged_in_client.post("/admin/run-now")
    assert r.status_code == 403


def test_run_now_launches_subprocess_when_idle(admin_client, monkeypatch):
    """When idle, Run Now must launch exactly one detached subprocess."""
    mock_popen = MagicMock()
    monkeypatch.setattr(subprocess, "Popen", mock_popen)
    r = admin_client.post("/admin/run-now", follow_redirects=False)
    assert r.status_code == 302
    mock_popen.assert_called_once()
    # Must be launched detached (start_new_session=True).
    _, kwargs = mock_popen.call_args
    assert kwargs.get("start_new_session") is True


def test_run_now_redirects_to_admin_runs(admin_client, monkeypatch):
    """Successful launch must redirect back to /admin/runs."""
    monkeypatch.setattr(subprocess, "Popen", MagicMock())
    r = admin_client.post("/admin/run-now", follow_redirects=False)
    assert "/admin/runs" in r.headers["Location"]


def test_run_now_flash_already_running_when_locked(admin_client, archive, monkeypatch):
    """When already running, must flash an info message instead of launching."""
    (archive / ".tubenews.lock").write_text(str(os.getpid()))
    mock_popen = MagicMock()
    monkeypatch.setattr(subprocess, "Popen", mock_popen)
    r = admin_client.post("/admin/run-now", follow_redirects=True)
    assert b"already running" in r.data.lower()
    mock_popen.assert_not_called()


def test_run_now_does_not_launch_when_locked(admin_client, archive, monkeypatch):
    """Subprocess must not be spawned if the lock is already held."""
    (archive / ".tubenews.lock").write_text(str(os.getpid()))
    mock_popen = MagicMock()
    monkeypatch.setattr(subprocess, "Popen", mock_popen)
    admin_client.post("/admin/run-now")
    mock_popen.assert_not_called()
