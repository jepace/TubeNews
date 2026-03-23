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


def test_channel_blog_includes_youtube_channel_link(logged_in_client):
    """The channel browse page must include a YouTube channel link containing the channel_id."""
    r = logged_in_client.get("/channel/UC_ALPHA_ID")
    assert b"UC_ALPHA_ID" in r.data
    assert b"youtube.com/channel/UC_ALPHA_ID" in r.data


def test_channel_blog_includes_rss_feed_link(logged_in_client):
    """The channel browse page must include an RSS feed link to the per-channel rss.xml."""
    r = logged_in_client.get("/channel/UC_ALPHA_ID")
    assert b"rss.xml" in r.data


# ---------------------------------------------------------------------------
# Admin all-stories blog (/admin/blog)
# ---------------------------------------------------------------------------

def test_admin_blog_requires_login(client, archive):
    r = client.get("/admin/blog")
    assert r.status_code == 302
    assert "/login" in r.headers["Location"]


def test_admin_blog_requires_admin(logged_in_client, archive):
    r = logged_in_client.get("/admin/blog")
    assert r.status_code == 403


def test_admin_blog_returns_200(admin_client, archive):
    r = admin_client.get("/admin/blog")
    assert r.status_code == 200


def test_admin_blog_shows_all_channel_stories(admin_client, archive):
    """All-stories view must include stories from every channel."""
    r = admin_client.get("/admin/blog")
    assert b"Alpha Council Approves Budget" in r.data
    assert b"Beta Council Discusses Zoning" in r.data


def test_admin_blog_links_to_aggregate_feed(admin_client, archive):
    """All-stories view must link to the aggregate RSS feed."""
    r = admin_client.get("/admin/blog")
    assert b"/archive/rss.xml" in r.data


# ---------------------------------------------------------------------------
# Admin story delete (/admin/story/delete)
# ---------------------------------------------------------------------------

_DELETE_FORM = {
    "channel_slug": "alpha_city",
    "meeting_id":   "2026-01-15_VID12345678",
    "filename":     "01_Story.md",
}


def test_admin_story_delete_requires_login(client, archive):
    r = client.post("/admin/story/delete", data=_DELETE_FORM)
    assert r.status_code == 302
    assert "/login" in r.headers["Location"]


def test_admin_story_delete_requires_admin(logged_in_client, archive):
    r = logged_in_client.post("/admin/story/delete", data=_DELETE_FORM)
    assert r.status_code == 403


def test_admin_story_delete_removes_file(admin_client, archive, monkeypatch):
    monkeypatch.setattr(webapp, "rebuild_feed",           lambda *a, **kw: None)
    monkeypatch.setattr(webapp, "rebuild_aggregate_feed", lambda *a, **kw: None)
    story_path = archive / "alpha_city" / "2026-01-15_VID12345678" / "01_Story.md"
    assert story_path.exists()
    r = admin_client.post("/admin/story/delete", data=_DELETE_FORM, follow_redirects=False)
    assert r.status_code == 302
    assert not story_path.exists()


def test_admin_story_delete_404_for_missing_file(admin_client, archive, monkeypatch):
    monkeypatch.setattr(webapp, "rebuild_feed",           lambda *a, **kw: None)
    monkeypatch.setattr(webapp, "rebuild_aggregate_feed", lambda *a, **kw: None)
    r = admin_client.post("/admin/story/delete", data={
        **_DELETE_FORM, "filename": "99_Does_Not_Exist.md",
    })
    assert r.status_code == 404


def test_admin_story_delete_rejects_non_md(admin_client, archive):
    r = admin_client.post("/admin/story/delete", data={
        **_DELETE_FORM, "filename": "metadata.json",
    })
    assert r.status_code == 400


def test_admin_story_delete_rejects_non_numbered_filename(admin_client, archive):
    r = admin_client.post("/admin/story/delete", data={
        **_DELETE_FORM, "filename": "story.md",
    })
    assert r.status_code == 400


def test_admin_story_delete_rejects_path_traversal(admin_client, archive):
    r = admin_client.post("/admin/story/delete", data={
        **_DELETE_FORM, "channel_slug": "../etc", "meeting_id": "passwd",
    })
    assert r.status_code == 400


def test_admin_blog_shows_delete_button_for_admin(admin_client, archive):
    r = admin_client.get("/admin/blog")
    assert b"admin/story/delete" in r.data


def test_blog_hides_delete_button_for_regular_user(logged_in_client, archive):
    r = logged_in_client.get("/blog")
    assert b"admin/story/delete" not in r.data


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


def test_admin_runs_channel_health_links_to_browse(admin_client, archive):
    """Channel names in the Channel Health table must link to /channel/<channel_id>."""
    r = admin_client.get("/admin/runs")
    assert r.status_code == 200
    assert b"/channel/UC_ALPHA_ID" in r.data
    assert b"/channel/UC_BETA__ID" in r.data


def test_admin_runs_run_history_links_to_browse(admin_client, archive):
    """Channel names in a run record's Channels column must link to /channel/<channel_id>."""
    import json as _json
    run_log = [{
        "started_at": 1741910400.0,
        "finished_at": 1741910460.0,
        "total_stories": 2,
        "ai_rate_limited": False,
        "feeds": [
            {"channel_id": "UC_ALPHA_ID", "channel_name": "Alpha City Council", "stories_written": 2},
            {"channel_id": "UC_BETA__ID", "channel_name": "Beta City Council",  "stories_written": 0},
        ],
    }]
    (archive / "run_log.json").write_text(_json.dumps(run_log))
    r = admin_client.get("/admin/runs")
    assert r.status_code == 200
    # Both channels should appear as links in the expandable run detail
    assert b"/channel/UC_ALPHA_ID" in r.data
    assert b"/channel/UC_BETA__ID" in r.data
    # Active channel should appear in the summary Channels column too
    assert b"Alpha City Council" in r.data


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


# ---------------------------------------------------------------------------
# admin_feed_edit — archive directory rename on channel name change
# ---------------------------------------------------------------------------
# The archive fixture creates alpha_city/ with channel.json {"channel_id":
# "UC_ALPHA_ID", ...}.  The code finds the old dir by scanning channel.json
# files (not by re-slugifying the current config name), so the dir is always
# found regardless of historical naming.

def test_feed_rename_moves_archive_dir(admin_client, archive):
    """Renaming a channel must rename the archive directory so the back catalog is preserved."""
    from TubeNews import slugify as _slugify
    old_dir = archive / "alpha_city"   # created by fixture
    assert old_dir.is_dir()
    new_name = "Alpha City Government"
    new_slug = _slugify(new_name)      # "Alpha_City_Government"

    admin_client.post("/admin/feeds/UC_ALPHA_ID/edit", data={
        "channel_id": "UC_ALPHA_ID",
        "channel_name": new_name,
        "focus": "housing",
    })

    assert (archive / new_slug).is_dir(), "new slug dir must exist"
    assert not old_dir.exists(), "old dir must be gone"


def test_feed_rename_updates_channel_json(admin_client, archive):
    """channel.json inside the renamed directory must reflect the new name."""
    import json as _json
    from TubeNews import slugify as _slugify
    new_name = "Alpha City Government"
    new_slug = _slugify(new_name)

    admin_client.post("/admin/feeds/UC_ALPHA_ID/edit", data={
        "channel_id": "UC_ALPHA_ID",
        "channel_name": new_name,
        "focus": "housing",
    })

    channel_json = archive / new_slug / "channel.json"
    assert channel_json.exists()
    data = _json.loads(channel_json.read_text())
    assert data["channel_name"] == new_name
    assert data["channel_id"] == "UC_ALPHA_ID"


def test_feed_rename_same_name_does_not_rename_dir(admin_client, archive):
    """Saving with the exact same channel_name must succeed without error."""
    r = admin_client.post("/admin/feeds/UC_ALPHA_ID/edit", data={
        "channel_id": "UC_ALPHA_ID",
        "channel_name": "Alpha City Council",   # same as config; slug differs from archive dir
        "focus": "housing",                      # but found via channel.json channel_id
    }, follow_redirects=False)
    assert r.status_code == 302   # redirect on success, no crash


def test_feed_rename_blocked_when_target_dir_exists(admin_client, archive):
    """Edit must flash an error and leave both dirs intact if new slug already exists."""
    from TubeNews import slugify as _slugify
    new_name = "Alpha City Government"
    new_slug = _slugify(new_name)
    (archive / new_slug).mkdir()   # pre-create collision

    r = admin_client.post("/admin/feeds/UC_ALPHA_ID/edit", data={
        "channel_id": "UC_ALPHA_ID",
        "channel_name": new_name,
        "focus": "housing",
    }, follow_redirects=True)

    assert b"already exists" in r.data
    assert (archive / "alpha_city").is_dir(), "original dir must survive collision"
    assert (archive / new_slug).is_dir()




# ---------------------------------------------------------------------------
# Dashboard subscription save — focuses stored as list, capped at 3
# ---------------------------------------------------------------------------

def test_dashboard_saves_focuses_as_list(logged_in_client, archive):
    """Focuses entered as newline-separated lines are saved as a list."""
    import json as _json
    import web.app as webapp

    r = logged_in_client.post("/dashboard", data={
        "channel_ids": ["UC_ALPHA_ID"],
        "focus_UC_ALPHA_ID": "housing, zoning\ntransportation, roads",
    }, follow_redirects=True)
    assert r.status_code == 200

    # Find the user in archive/users and check channel_focus
    users_dir = webapp.STORAGE_ROOT / "users"
    user_data = None
    for uid_dir in users_dir.iterdir():
        uj = uid_dir / "user.json"
        if uj.exists():
            d = _json.loads(uj.read_text())
            if d.get("email") == "test@example.com":
                user_data = d
                break
    assert user_data is not None
    focus_val = user_data["channel_focus"]["UC_ALPHA_ID"]
    assert isinstance(focus_val, list)
    assert "housing, zoning" in focus_val
    assert "transportation, roads" in focus_val


def test_dashboard_caps_focuses_at_three(logged_in_client, archive):
    """A fourth focus line is silently dropped."""
    import json as _json
    import web.app as webapp

    r = logged_in_client.post("/dashboard", data={
        "channel_ids": ["UC_ALPHA_ID"],
        "focus_UC_ALPHA_ID": "focus one\nfocus two\nfocus three\nfocus four",
    }, follow_redirects=True)
    assert r.status_code == 200

    users_dir = webapp.STORAGE_ROOT / "users"
    for uid_dir in users_dir.iterdir():
        uj = uid_dir / "user.json"
        if uj.exists():
            d = _json.loads(uj.read_text())
            if d.get("email") == "test@example.com":
                assert len(d["channel_focus"]["UC_ALPHA_ID"]) == 3
                return
    pytest.fail("User not found")


# ---------------------------------------------------------------------------
# Unseen-channel nav badge
# ---------------------------------------------------------------------------

def test_dashboard_get_initialises_seen_channel_ids(logged_in_client, archive):
    """GET /dashboard writes seen_channel_ids covering all configured channels."""
    import json as _json
    import web.app as webapp

    logged_in_client.get("/dashboard")

    users_dir = webapp.STORAGE_ROOT / "users"
    for uid_dir in users_dir.iterdir():
        uj = uid_dir / "user.json"
        if not uj.exists():
            continue
        d = _json.loads(uj.read_text())
        if d.get("email") == "test@example.com":
            assert set(d["seen_channel_ids"]) == {"UC_ALPHA_ID", "UC_BETA__ID"}
            return
    pytest.fail("User not found")


def test_dashboard_post_sets_seen_channel_ids(logged_in_client, archive):
    """POST /dashboard includes seen_channel_ids in the save."""
    import json as _json
    import web.app as webapp

    logged_in_client.post("/dashboard", data={"channel_ids": ["UC_ALPHA_ID"]},
                          follow_redirects=True)

    users_dir = webapp.STORAGE_ROOT / "users"
    for uid_dir in users_dir.iterdir():
        uj = uid_dir / "user.json"
        if not uj.exists():
            continue
        d = _json.loads(uj.read_text())
        if d.get("email") == "test@example.com":
            assert set(d["seen_channel_ids"]) == {"UC_ALPHA_ID", "UC_BETA__ID"}
            return
    pytest.fail("User not found")


def test_nav_badge_shown_when_unseen_channel_exists(client, archive):
    """Nav badge appears when a channel is not in user's seen_channel_ids."""
    import json as _json
    import web.app as webapp

    # Create user subscribed to Alpha who has only "seen" Alpha (Beta is new to them)
    users_dir = webapp.STORAGE_ROOT / "users"
    _make_user(
        users_dir, name="Partial User", email="partial@example.com",
        channel_ids=["UC_ALPHA_ID"], token="partial-token-xyz",
    )
    for uid_dir in users_dir.iterdir():
        uj = uid_dir / "user.json"
        if not uj.exists():
            continue
        d = _json.loads(uj.read_text())
        if d.get("email") == "partial@example.com":
            d["seen_channel_ids"] = ["UC_ALPHA_ID"]
            uj.write_text(_json.dumps(d))
            break

    client.post("/login", data={"email": "partial@example.com", "password": "testpassword123"})
    # /blog renders for this user (they have a subscription); badge should appear
    r = client.get("/blog")
    assert b'nav-badge' in r.data
    assert b'>1<' in r.data


def test_nav_badge_hidden_when_seen_channel_ids_absent(logged_in_client, archive):
    """No badge when seen_channel_ids key is absent (existing-user migration path)."""
    r = logged_in_client.get("/blog")
    assert b'nav-badge' not in r.data


def test_nav_badge_hidden_after_dashboard_visit(client, archive):
    """Badge disappears once the user visits /dashboard (marks all as seen)."""
    import json as _json
    import web.app as webapp

    # Set up user with only Alpha seen
    users_dir = webapp.STORAGE_ROOT / "users"
    _make_user(users_dir, name="Watcher", email="watcher@example.com",
               channel_ids=["UC_ALPHA_ID"], token="watcher-token")
    for uid_dir in users_dir.iterdir():
        uj = uid_dir / "user.json"
        if not uj.exists():
            continue
        d = _json.loads(uj.read_text())
        if d.get("email") == "watcher@example.com":
            d["seen_channel_ids"] = ["UC_ALPHA_ID"]
            uj.write_text(_json.dumps(d))
            break

    client.post("/login", data={"email": "watcher@example.com", "password": "testpassword123"})

    # Badge present before visiting dashboard (/blog renders since user has a subscription)
    r = client.get("/blog")
    assert b'nav-badge' in r.data

    # Visit dashboard — clears the badge
    client.get("/dashboard")

    # Badge gone on subsequent page load
    r = client.get("/blog")
    assert b'nav-badge' not in r.data


# ---------------------------------------------------------------------------
# Security: serve_archive must not expose user data
# ---------------------------------------------------------------------------

def test_serve_archive_blocks_users_root(client, archive):
    """/archive/users/ must return 404, not expose the directory."""
    r = client.get("/archive/users")
    assert r.status_code == 404


def test_serve_archive_blocks_users_subpath(client, archive, registered_user):
    """/archive/users/<uuid>/user.json must return 404."""
    users_dir = webapp.STORAGE_ROOT / "users"
    user_uuid = next(users_dir.iterdir()).name
    r = client.get(f"/archive/users/{user_uuid}/user.json")
    assert r.status_code == 404


def test_serve_archive_allows_rss_feed(client, archive):
    """/archive/rss.xml is still accessible (if the file exists)."""
    (archive / "rss.xml").write_text("<rss/>")
    r = client.get("/archive/rss.xml")
    assert r.status_code == 200


# ---------------------------------------------------------------------------
# Security: serve_transcript must block path traversal
# ---------------------------------------------------------------------------

def test_serve_transcript_blocks_dotdot_in_slug(client, archive):
    """.. in channel_slug must not traverse above STORAGE_ROOT."""
    r = client.get("/transcript/../something/meeting_id")
    # Flask routes reject '..' segments in the URL; we get 404 either way
    assert r.status_code in (400, 404)


def test_serve_transcript_blocks_dotdot_in_meeting(client, archive):
    """.. in meeting_id must not traverse above STORAGE_ROOT."""
    # Create a sentinel transcript one level above the archive in tmp
    sentinel = archive.parent / "transcript.txt"
    sentinel.write_text("0s --> secret content\n")
    try:
        r = client.get(f"/transcript/alpha_city/..%2F..")
        assert r.status_code in (400, 404)
    finally:
        sentinel.unlink(missing_ok=True)


def test_serve_transcript_valid_route_still_works(client, archive):
    """A legitimate transcript URL must continue to return 200."""
    channel_dir = archive / "alpha_city"
    meeting_dir = channel_dir / "2026-01-15_VID12345678"
    meeting_dir.mkdir(parents=True, exist_ok=True)
    (meeting_dir / "transcript.txt").write_text("120s --> Hello world\n")
    r = client.get("/transcript/alpha_city/2026-01-15_VID12345678")
    assert r.status_code == 200


# ---------------------------------------------------------------------------
# Security: login ?next= open-redirect prevention
# ---------------------------------------------------------------------------

def test_login_next_blocks_absolute_url(client, archive, registered_user):
    """?next=https://evil.com must not redirect off-site after login."""
    r = client.post(
        "/login?next=https://evil.com",
        data={"email": "test@example.com", "password": "testpassword123"},
    )
    assert r.status_code == 302
    assert "evil.com" not in r.headers["Location"]


def test_login_next_blocks_protocol_relative_url(client, archive, registered_user):
    """?next=//evil.com must not redirect off-site after login."""
    r = client.post(
        "/login?next=//evil.com",
        data={"email": "test@example.com", "password": "testpassword123"},
    )
    assert r.status_code == 302
    assert "evil.com" not in r.headers["Location"]


def test_login_next_allows_local_path(client, archive, registered_user):
    """?next=/dashboard must redirect to that local path after login."""
    r = client.post(
        "/login?next=/dashboard",
        data={"email": "test@example.com", "password": "testpassword123"},
    )
    assert r.status_code == 302
    assert r.headers["Location"].endswith("/dashboard")


# ---------------------------------------------------------------------------
# ntfy notifications
# ---------------------------------------------------------------------------

def test_register_sends_ntfy(client, archive, monkeypatch):
    """Successful registration fires a ntfy notification."""
    sent = []
    monkeypatch.setattr(webapp, "_web_ntfy", lambda title, msg, **kw: sent.append((title, msg)))
    client.post("/register", data={
        "name": "Alice",
        "email": "alice@example.com",
        "password": "securepassword1",
        "confirm_password": "securepassword1",
    })
    assert len(sent) == 1
    assert "new user" in sent[0][0].lower()
    assert "alice@example.com" in sent[0][1]


def test_register_no_ntfy_on_failure(client, archive, monkeypatch):
    """A failed registration (bad password) must not fire a ntfy notification."""
    sent = []
    monkeypatch.setattr(webapp, "_web_ntfy", lambda title, msg, **kw: sent.append((title, msg)))
    client.post("/register", data={
        "name": "Alice",
        "email": "alice@example.com",
        "password": "short",
        "confirm_password": "short",
    })
    assert sent == []


def test_run_now_sends_ntfy(archive, monkeypatch):
    """Admin triggering a manual run fires a ntfy notification."""
    import json as _json
    from werkzeug.security import generate_password_hash as _gph

    # Create an admin user
    users_dir = archive / "users"
    uid = str(uuid.uuid4())
    (users_dir / uid).mkdir()
    (users_dir / uid / "user.json").write_text(_json.dumps({
        "name": "Admin",
        "email": "admin@example.com",
        "password_hash": _gph("adminpassword1"),
        "channel_ids": [],
        "feed_token": str(uuid.uuid4()),
        "created_at": int(time.time()),
    }))
    import web.app as _wa
    cfg_path = _wa.CONFIG_FILE
    cfg = _json.loads(cfg_path.read_text())
    cfg["admin_users"] = ["admin@example.com"]
    cfg_path.write_text(_json.dumps(cfg))

    sent = []
    monkeypatch.setattr(webapp, "_web_ntfy", lambda title, msg, **kw: sent.append((title, msg)))
    monkeypatch.setattr(webapp, "_is_running", lambda: False)
    monkeypatch.setattr(webapp.subprocess, "Popen", lambda *a, **kw: None)

    flask_app.config["TESTING"] = True
    flask_app.config["WTF_CSRF_ENABLED"] = False
    with flask_app.test_client() as c:
        c.post("/login", data={"email": "admin@example.com", "password": "adminpassword1"})
        c.post("/admin/run-now")

    assert len(sent) == 1
    assert "run started" in sent[0][0].lower()
