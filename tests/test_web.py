"""Unit tests for web/app.py helpers.

Focus areas:
- URL generation (_rss_url, _feed_url): must return relative paths when
  base_url is not configured, and absolute paths only when it is.  This
  has broken twice — once with _external=True producing https:// links
  when HTTPS was not set up.  These tests are the regression guard.
- Display preferences (_prefs_to_classes): maps user prefs dict to the
  HTML class string applied to <html>.
- Flask route integration: hit real routes via test_client and verify
  the HTML rendered to the browser never contains absolute https:// URLs
  in feed/rss URL fields.  Helper-function unit tests alone are not
  enough — they don't catch bugs in how routes call or pass those helpers.

Import note: app.py reads TubeNews.json at import time for the secret
key.  We set TUBENEWS_SECRET_KEY in the environment before importing so
the tests work even without a local TubeNews.json.
"""

import json
import os
import sys
import time
import uuid
from pathlib import Path
from unittest.mock import patch

import pytest
from werkzeug.security import generate_password_hash

# Must be set before importing the web module so the secret-key check passes.
os.environ.setdefault("TUBENEWS_SECRET_KEY", "test-secret-key-for-testing-only-xx")

# Add the web/ directory so we can `import app` directly.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "web"))

from app import _rss_url, _feed_url, _prefs_to_classes  # noqa: E402

# Add parent to path for TubeNews imports
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from TubeNews import now_utc_iso  # noqa: E402


# ---------------------------------------------------------------------------
# _rss_url — no base_url configured
# ---------------------------------------------------------------------------

def test_rss_url_no_base_is_root_relative():
    """Without base_url the RSS URL must be root-relative, not absolute."""
    with patch("app._base_url", return_value=""):
        url = _rss_url("mytoken")
    assert url == "/feed/mytoken.xml"


def test_rss_url_no_base_has_no_scheme():
    """Without base_url the URL must not contain a scheme (http/https)."""
    with patch("app._base_url", return_value=""):
        url = _rss_url("mytoken")
    assert not url.startswith("http")


def test_rss_url_no_base_no_hostname():
    """Without base_url the URL must not embed a hostname."""
    with patch("app._base_url", return_value=""):
        url = _rss_url("tok123")
    assert "localhost" not in url
    assert "127.0.0.1" not in url
    assert "://" not in url


def test_rss_url_ends_with_xml():
    with patch("app._base_url", return_value=""):
        assert _rss_url("tok").endswith(".xml")


def test_rss_url_contains_token():
    token = "abc123def456"
    with patch("app._base_url", return_value=""):
        assert token in _rss_url(token)


# ---------------------------------------------------------------------------
# _rss_url — base_url configured
# ---------------------------------------------------------------------------

def test_rss_url_with_base_uses_base():
    with patch("app._base_url", return_value="https://example.com"):
        url = _rss_url("mytoken")
    assert url == "https://example.com/feed/mytoken.xml"


def test_rss_url_with_base_no_double_slash():
    with patch("app._base_url", return_value="https://example.com"):
        url = _rss_url("tok")
    assert "//" not in url.replace("https://", "")


# ---------------------------------------------------------------------------
# _feed_url — no base_url configured
# ---------------------------------------------------------------------------

def test_feed_url_no_base_is_root_relative():
    """Without base_url the feed URL must be root-relative, not absolute."""
    with patch("app._base_url", return_value=""):
        url = _feed_url("mytoken")
    assert url == "/feed/mytoken.html"


def test_feed_url_no_base_has_no_scheme():
    """Without base_url the URL must not contain a scheme (http/https)."""
    with patch("app._base_url", return_value=""):
        url = _feed_url("mytoken")
    assert not url.startswith("http")


def test_feed_url_no_base_no_hostname():
    """Without base_url the URL must not embed a hostname."""
    with patch("app._base_url", return_value=""):
        url = _feed_url("tok123")
    assert "localhost" not in url
    assert "127.0.0.1" not in url
    assert "://" not in url


def test_feed_url_ends_with_html():
    with patch("app._base_url", return_value=""):
        assert _feed_url("tok").endswith(".html")


def test_feed_url_contains_token():
    token = "abc123def456"
    with patch("app._base_url", return_value=""):
        assert token in _feed_url(token)


# ---------------------------------------------------------------------------
# _feed_url — base_url configured
# ---------------------------------------------------------------------------

def test_feed_url_with_base_uses_base():
    with patch("app._base_url", return_value="https://example.com"):
        url = _feed_url("mytoken")
    assert url == "https://example.com/feed/mytoken.html"


def test_feed_url_with_base_no_double_slash():
    with patch("app._base_url", return_value="https://example.com"):
        url = _feed_url("tok")
    assert "//" not in url.replace("https://", "")


# ---------------------------------------------------------------------------
# RSS and feed URL symmetry
# ---------------------------------------------------------------------------

def test_rss_and_feed_urls_share_token():
    """The same token must appear in both the RSS and feed URLs."""
    token = "shared-token-xyz"
    with patch("app._base_url", return_value=""):
        rss = _rss_url(token)
        feed = _feed_url(token)
    assert token in rss
    assert token in feed


def test_rss_and_feed_urls_differ():
    """RSS and feed URLs must be distinct paths."""
    with patch("app._base_url", return_value=""):
        assert _rss_url("tok") != _feed_url("tok")


# ---------------------------------------------------------------------------
# _prefs_to_classes — empty / default
# ---------------------------------------------------------------------------

def test_prefs_empty_dict_returns_empty_string():
    assert _prefs_to_classes({}) == ""


def test_prefs_all_defaults_returns_empty_string():
    assert _prefs_to_classes({"dark_mode": False, "font_size": "normal"}) == ""


# ---------------------------------------------------------------------------
# _prefs_to_classes — dark mode
# ---------------------------------------------------------------------------

def test_prefs_dark_mode_true_adds_dark_class():
    assert "dark" in _prefs_to_classes({"dark_mode": True}).split()


def test_prefs_dark_mode_false_no_dark_class():
    assert "dark" not in _prefs_to_classes({"dark_mode": False}).split()


def test_prefs_dark_mode_missing_no_dark_class():
    assert "dark" not in _prefs_to_classes({}).split()


# ---------------------------------------------------------------------------
# _prefs_to_classes — font size
# ---------------------------------------------------------------------------

def test_prefs_font_large_adds_font_large_class():
    assert "font-large" in _prefs_to_classes({"font_size": "large"}).split()


def test_prefs_font_larger_adds_font_larger_class():
    assert "font-larger" in _prefs_to_classes({"font_size": "larger"}).split()


def test_prefs_font_normal_adds_no_font_class():
    result = _prefs_to_classes({"font_size": "normal"})
    assert "font-" not in result


def test_prefs_font_missing_adds_no_font_class():
    assert "font-" not in _prefs_to_classes({})


def test_prefs_invalid_font_size_adds_no_font_class():
    """An unrecognised font_size value must not produce a CSS class."""
    result = _prefs_to_classes({"font_size": "gigantic"})
    assert "font-" not in result


# ---------------------------------------------------------------------------
# _prefs_to_classes — combined
# ---------------------------------------------------------------------------

def test_prefs_dark_and_large_both_present():
    result = _prefs_to_classes({"dark_mode": True, "font_size": "large"})
    parts = result.split()
    assert "dark" in parts
    assert "font-large" in parts


def test_prefs_dark_and_larger_both_present():
    result = _prefs_to_classes({"dark_mode": True, "font_size": "larger"})
    parts = result.split()
    assert "dark" in parts
    assert "font-larger" in parts


def test_prefs_classes_are_space_separated():
    result = _prefs_to_classes({"dark_mode": True, "font_size": "large"})
    # Must be a valid HTML class string — no leading/trailing whitespace,
    # individual tokens separated by single spaces.
    assert result == result.strip()
    assert "  " not in result


# ---------------------------------------------------------------------------
# Flask route integration tests
#
# These tests make real HTTP requests through app.test_client() and inspect
# the rendered HTML.  Unit tests on _rss_url/_feed_url are necessary but
# not sufficient: they don't catch bugs where a route calls the wrong helper,
# passes the result incorrectly, or a template renders a different variable.
# ---------------------------------------------------------------------------

import app as _web_app   # already imported above; alias for clarity


@pytest.fixture()
def flask_env(tmp_path, monkeypatch):
    """Minimal Flask test environment: one admin user, one regular user.

    - WTF_CSRF_ENABLED disabled so POST forms work without tokens.
    - CONFIG_FILE points to a temp TubeNews.json with no base_url set,
      so _rss_url/_feed_url must return root-relative paths.
    - USERS_ROOT points to tmp_path/_users.
    - Admin is logged in via the real /login route before yielding.
    """
    _web_app.app.config["TESTING"] = True
    _web_app.app.config["WTF_CSRF_ENABLED"] = False

    # Config with no base_url — all generated URLs must be relative.
    config_file = tmp_path / "TubeNews.json"
    config_file.write_text(json.dumps({
        "tubenews_key": "test-secret-key-for-testing-only-xx",
        "admin_users": ["admin@test.com"],
        "feeds": [],
        "base_url": "",
    }))
    monkeypatch.setattr(_web_app, "CONFIG_FILE", config_file)

    users_root = tmp_path / "_users"
    users_root.mkdir()
    monkeypatch.setattr(_web_app, "USERS_ROOT", users_root)

    # Admin user
    admin_id = str(uuid.uuid4())
    admin_dir = users_root / admin_id
    admin_dir.mkdir()
    (admin_dir / "user.json").write_text(json.dumps({
        "name": "Admin User",
        "email": "admin@test.com",
        "password_hash": generate_password_hash("adminpassword1"),
        "channel_ids": [],
        "feed_token": "admin-feed-token-xyz",
        "created_at": now_utc_iso(),
        "locked": False,
    }))

    # Regular target user whose edit page we'll inspect
    target_id = str(uuid.uuid4())
    target_dir = users_root / target_id
    target_dir.mkdir()
    (target_dir / "user.json").write_text(json.dumps({
        "name": "Target User",
        "email": "target@test.com",
        "password_hash": generate_password_hash("targetpassword1"),
        "channel_ids": [],
        "feed_token": "target-feed-token-abc",
        "created_at": now_utc_iso(),
        "locked": False,
    }))

    with _web_app.app.test_client() as client:
        # Log in as admin via the real login route.
        client.post("/login", data={
            "email": "admin@test.com",
            "password": "adminpassword1",
        }, follow_redirects=True)
        yield client, admin_id, target_id


# ── Admin edit-user page ──────────────────────────────────────────────────

def test_admin_edit_user_rss_url_is_root_relative(flask_env):
    """RSS feed URL shown on admin edit-user page must be a root-relative path."""
    client, _, target_id = flask_env
    html = client.get(f"/admin/user/{target_id}").data.decode()
    assert 'value="/feed/target-feed-token-abc.xml"' in html


def test_admin_edit_user_feed_url_is_root_relative(flask_env):
    """Feed (story page) URL shown on admin edit-user page must be a root-relative path."""
    client, _, target_id = flask_env
    html = client.get(f"/admin/user/{target_id}").data.decode()
    assert 'value="/feed/target-feed-token-abc.html"' in html


def test_admin_edit_user_rss_url_no_https(flask_env):
    """RSS URL on admin edit-user page must never contain https:// when base_url is unset."""
    client, _, target_id = flask_env
    html = client.get(f"/admin/user/{target_id}").data.decode()
    # Find the RSS URL input value and assert it's not absolute.
    assert "https://localhost" not in html
    assert "https://127" not in html
    # The token must appear in a relative context only.
    token = "target-feed-token-abc"
    idx = html.find(token)
    assert idx != -1
    # Check the 50 chars before the token for a scheme — there must be none.
    prefix = html[max(0, idx - 50):idx]
    assert "https://" not in prefix
    assert "http://" not in prefix


def test_admin_edit_user_feed_url_no_https(flask_env):
    """Feed URL on admin edit-user page must never contain https:// when base_url is unset."""
    client, _, target_id = flask_env
    html = client.get(f"/admin/user/{target_id}").data.decode()
    token = "target-feed-token-abc"
    # Find the second occurrence (feed URL comes after RSS URL).
    first = html.find(token)
    idx = html.find(token, first + 1)
    assert idx != -1
    prefix = html[max(0, idx - 50):idx]
    assert "https://" not in prefix
    assert "http://" not in prefix


def test_admin_edit_user_page_loads(flask_env):
    """Sanity: admin edit-user page must return HTTP 200."""
    client, _, target_id = flask_env
    resp = client.get(f"/admin/user/{target_id}")
    assert resp.status_code == 200


# ── Account page ──────────────────────────────────────────────────────────

def test_account_feed_url_is_root_relative(flask_env):
    """RSS feed URL on the account page must be root-relative when base_url is unset."""
    client, admin_id, _ = flask_env
    # Give the admin a subscription so the feed URL section renders.
    import app as wa
    user = wa._find_user_by_id(admin_id)
    # Patch channel list so a valid channel_id exists to subscribe to.
    with patch("app._load_channels", return_value=[
        {"channel_id": "UC_TEST_CHAN", "channel_name": "Test Channel", "focus": "test"}
    ]):
        user._data["channels"] = {"UC_TEST_CHAN": []}
        user._save()
        resp = client.get("/account")
    html = resp.data.decode()
    assert "/feed/admin-feed-token-xyz.xml" in html
    assert "https://localhost" not in html
    assert "https://127" not in html


def test_account_feed_page_url_is_root_relative(flask_env):
    """Feed (story page) URL on the account page must be root-relative when base_url is unset."""
    client, admin_id, _ = flask_env
    import app as wa
    user = wa._find_user_by_id(admin_id)
    with patch("app._load_channels", return_value=[
        {"channel_id": "UC_TEST_CHAN", "channel_name": "Test Channel", "focus": "test"}
    ]):
        user._data["channels"] = {"UC_TEST_CHAN": []}
        user._save()
        resp = client.get("/account")
    html = resp.data.decode()
    assert "/feed/admin-feed-token-xyz.html" in html
    assert "https://localhost" not in html
    assert "https://127" not in html


# ── base_url configured → absolute URLs ──────────────────────────────────

def test_admin_edit_user_feed_url_uses_configured_base_url(flask_env, tmp_path):
    """When base_url IS set, the feed URL must be absolute using that base."""
    client, _, target_id = flask_env
    import app as wa
    # Rewrite config with a base_url.
    config_file = tmp_path / "TubeNews.json"
    config_file.write_text(json.dumps({
        "tubenews_key": "test-secret-key-for-testing-only-xx",
        "admin_users": ["admin@test.com"],
        "feeds": [],
        "base_url": "https://news.example.com",
    }))
    html = client.get(f"/admin/user/{target_id}").data.decode()
    assert "https://news.example.com/feed/target-feed-token-abc.xml" in html


def test_admin_edit_user_feed_page_url_uses_configured_base_url(flask_env, tmp_path):
    """When base_url IS set, the feed page URL must be absolute using that base."""
    client, _, target_id = flask_env
    import app as wa
    config_file = tmp_path / "TubeNews.json"
    config_file.write_text(json.dumps({
        "tubenews_key": "test-secret-key-for-testing-only-xx",
        "admin_users": ["admin@test.com"],
        "feeds": [],
        "base_url": "https://news.example.com",
    }))
    html = client.get(f"/admin/user/{target_id}").data.decode()
    assert "https://news.example.com/feed/target-feed-token-abc.html" in html


def test_public_article_page_loads(flask_env):
    """Public article page should load without login."""
    client, tmp_path, _ = flask_env
    # Create a minimal story file
    storage = tmp_path / "content" / "test_channel" / "2024-04-21"
    storage.mkdir(parents=True)

    metadata = storage / "metadata.json"
    metadata.write_text(json.dumps({
        "video_id": "test_vid_123",
        "video_title": "Test Video",
        "channel_id": "UC_test",
        "processed_at": "2024-04-21T10:00:00Z",
        "status": "ok"
    }))

    story_file = storage / "01_Test_Story.md"
    story_file.write_text("""# Test Article Title
*April 21, 2024*
**Source:** https://youtu.be/test_vid_123?t=120

This is a test article body.

---
**Segment Start:** 120s
**Topics:** test, demo
Published 2024-04-21T10:00:00Z
Video published 2024-04-21T10:00:00Z
""")

    # Request the public article page
    resp = client.get("/article/test_vid_123/120")
    assert resp.status_code == 200
    html = resp.data.decode()
    assert "Test Article Title" in html
    assert "This is a test article body" in html


def test_public_article_404_on_missing_video(flask_env):
    """Public article page should 404 if video_id doesn't exist."""
    client, tmp_path, _ = flask_env
    resp = client.get("/article/nonexistent_video/120")
    assert resp.status_code == 404


def test_public_article_404_on_missing_start_seconds(flask_env):
    """Public article page should 404 if start_seconds doesn't match any story."""
    client, tmp_path, _ = flask_env
    # Create a story file
    storage = tmp_path / "content" / "test_channel" / "2024-04-21"
    storage.mkdir(parents=True)

    metadata = storage / "metadata.json"
    metadata.write_text(json.dumps({
        "video_id": "test_vid_123",
        "video_title": "Test Video",
        "channel_id": "UC_test",
        "processed_at": "2024-04-21T10:00:00Z",
        "status": "ok"
    }))

    story_file = storage / "01_Test_Story.md"
    story_file.write_text("""# Test Article
*April 21, 2024*
**Source:** https://youtu.be/test_vid_123?t=120

Test body.

---
**Segment Start:** 120s
Published 2024-04-21T10:00:00Z
""")

    # Request with matching video_id but different start_seconds
    resp = client.get("/article/test_vid_123/999")
    assert resp.status_code == 404
