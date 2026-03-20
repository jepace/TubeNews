"""Unit tests for web/app.py helpers.

Focus areas:
- URL generation (_feed_url, _blog_url): must return relative paths when
  base_url is not configured, and absolute paths only when it is.  This
  has broken twice — once with _external=True producing https:// links
  when HTTPS was not set up.  These tests are the regression guard.
- Display preferences (_prefs_to_classes): maps user prefs dict to the
  HTML class string applied to <html>.

Import note: app.py reads TubeNews.json at import time for the secret
key.  We set TUBENEWS_SECRET_KEY in the environment before importing so
the tests work even without a local TubeNews.json.
"""

import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

# Must be set before importing the web module so the secret-key check passes.
os.environ.setdefault("TUBENEWS_SECRET_KEY", "test-secret-key-for-testing-only-xx")

# Add the web/ directory so we can `import app` directly.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "web"))

from app import _feed_url, _blog_url, _prefs_to_classes  # noqa: E402


# ---------------------------------------------------------------------------
# _feed_url — no base_url configured
# ---------------------------------------------------------------------------

def test_feed_url_no_base_is_root_relative():
    """Without base_url the feed URL must be root-relative, not absolute."""
    with patch("app._base_url", return_value=""):
        url = _feed_url("mytoken")
    assert url == "/feed/mytoken.xml"


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


def test_feed_url_ends_with_xml():
    with patch("app._base_url", return_value=""):
        assert _feed_url("tok").endswith(".xml")


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
    assert url == "https://example.com/feed/mytoken.xml"


def test_feed_url_with_base_no_double_slash():
    with patch("app._base_url", return_value="https://example.com"):
        url = _feed_url("tok")
    assert "//" not in url.replace("https://", "")


# ---------------------------------------------------------------------------
# _blog_url — no base_url configured
# ---------------------------------------------------------------------------

def test_blog_url_no_base_is_root_relative():
    """Without base_url the blog URL must be root-relative, not absolute."""
    with patch("app._base_url", return_value=""):
        url = _blog_url("mytoken")
    assert url == "/blog/mytoken.html"


def test_blog_url_no_base_has_no_scheme():
    """Without base_url the URL must not contain a scheme (http/https)."""
    with patch("app._base_url", return_value=""):
        url = _blog_url("mytoken")
    assert not url.startswith("http")


def test_blog_url_no_base_no_hostname():
    """Without base_url the URL must not embed a hostname."""
    with patch("app._base_url", return_value=""):
        url = _blog_url("tok123")
    assert "localhost" not in url
    assert "127.0.0.1" not in url
    assert "://" not in url


def test_blog_url_ends_with_html():
    with patch("app._base_url", return_value=""):
        assert _blog_url("tok").endswith(".html")


def test_blog_url_contains_token():
    token = "abc123def456"
    with patch("app._base_url", return_value=""):
        assert token in _blog_url(token)


# ---------------------------------------------------------------------------
# _blog_url — base_url configured
# ---------------------------------------------------------------------------

def test_blog_url_with_base_uses_base():
    with patch("app._base_url", return_value="https://example.com"):
        url = _blog_url("mytoken")
    assert url == "https://example.com/blog/mytoken.html"


def test_blog_url_with_base_no_double_slash():
    with patch("app._base_url", return_value="https://example.com"):
        url = _blog_url("tok")
    assert "//" not in url.replace("https://", "")


# ---------------------------------------------------------------------------
# Feed and blog URL symmetry
# ---------------------------------------------------------------------------

def test_feed_and_blog_urls_share_token():
    """The same token must appear in both the feed and blog URLs."""
    token = "shared-token-xyz"
    with patch("app._base_url", return_value=""):
        feed = _feed_url(token)
        blog = _blog_url(token)
    assert token in feed
    assert token in blog


def test_feed_and_blog_urls_differ():
    """Feed and blog URLs must be distinct paths."""
    with patch("app._base_url", return_value=""):
        assert _feed_url("tok") != _blog_url("tok")


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
