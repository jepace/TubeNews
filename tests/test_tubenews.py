"""Unit tests for TubeNews.py — run with: pytest tests/ -v"""
import json
import re
import sys
import time
from pathlib import Path

import pytest

# Make the project root importable
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from TubeNews import slugify, rebuild_feed, rebuild_meta_feed


# ---------------------------------------------------------------------------
# slugify
# ---------------------------------------------------------------------------

def test_slugify_spaces():
    assert slugify("City Council") == "City_Council"

def test_slugify_special_chars():
    assert slugify("Hello, World!") == "Hello__World"

def test_slugify_leading_trailing_special():
    assert slugify("---test---") == "test"

def test_slugify_numbers_preserved():
    assert slugify("AB12 cd") == "AB12_cd"

def test_slugify_empty_string():
    assert slugify("") == ""

def test_slugify_only_specials():
    assert slugify("---") == ""


# ---------------------------------------------------------------------------
# JSON story extraction regex (from generate_news)
# ---------------------------------------------------------------------------

STORY_REGEX = re.compile(r'\[\s*{.*}\s*\]', re.DOTALL)

def test_json_extraction_clean():
    raw = '[{"title": "Test Story", "content": "Body text"}]'
    match = STORY_REGEX.search(raw)
    assert match is not None
    stories = json.loads(match.group(0))
    assert stories[0]['title'] == 'Test Story'

def test_json_extraction_prose_wrapped():
    raw = 'Here are the stories:\n[{"title": "Test", "content": "Body"}]\nEnd of report.'
    match = STORY_REGEX.search(raw)
    assert match is not None
    stories = json.loads(match.group(0))
    assert len(stories) == 1
    assert stories[0]['title'] == 'Test'

def test_json_extraction_multiple_stories():
    raw = '[{"title": "A", "content": "Aa"}, {"title": "B", "content": "Bb"}]'
    match = STORY_REGEX.search(raw)
    stories = json.loads(match.group(0))
    assert len(stories) == 2

def test_json_extraction_no_match():
    raw = "No JSON list here, just plain text."
    match = STORY_REGEX.search(raw)
    assert match is None


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _make_story_file(meeting_dir: Path, filename: str, title: str, dateline: str,
                     content: str, start_seconds: int = 60):
    path = meeting_dir / filename
    path.write_text(
        f"# {title}\n"
        f"*{dateline}*\n\n"
        f"{content}\n\n"
        f"---\n"
        f"**Segment Start:** {start_seconds}s\n",
        encoding='utf-8'
    )
    return path


def _make_meeting(feed_dir: Path, date_prefix: str, video_id: str,
                  title: str, status: str = "processed") -> Path:
    meeting_dir = feed_dir / f"{date_prefix}_{video_id}"
    meeting_dir.mkdir(parents=True, exist_ok=True)
    meta = {
        "video_id": video_id,
        "video_title": title,
        "status": status,
        "processed_at": int(time.time()),
    }
    (meeting_dir / "metadata.json").write_text(json.dumps(meta))
    return meeting_dir


@pytest.fixture
def council_feed(tmp_path):
    """A minimal council feed directory with one processed meeting and one ignored."""
    feed_dir = tmp_path / "test_council"
    feed_dir.mkdir()

    # Processed meeting with two stories
    m1 = _make_meeting(feed_dir, "2026-01-15", "VALID1234567", "Council Meeting Jan 15")
    _make_story_file(m1, "01_Housing.md", "New Housing Plan Approved",
                     "TESTVILLE, Calif. — January 15, 2026", "Council approved 50 units.", 120)
    _make_story_file(m1, "02_Budget.md", "Budget Amendment Passes",
                     "TESTVILLE, Calif. — January 15, 2026", "A $500k amendment passed.", 300)

    # Ignored (too old) meeting with a story that must NOT appear in feed
    m2 = _make_meeting(feed_dir, "1900-01-01", "OLD01234567", "Old Meeting", status="ignored_too_old")
    _make_story_file(m2, "01_Old.md", "Should Not Appear",
                     "TESTVILLE, Calif. — January 1, 1900", "Old content.", 0)

    return feed_dir


@pytest.fixture
def feed_cfg():
    return {
        'channel_name': 'Test Council',
        'channel_id': 'UCtest1234567890',
        'focus': 'housing and zoning',
    }


# ---------------------------------------------------------------------------
# rebuild_feed
# ---------------------------------------------------------------------------

def test_rebuild_feed_creates_rss(council_feed, feed_cfg):
    rebuild_feed(council_feed, feed_cfg)
    assert (council_feed / "rss.xml").exists()

def test_rebuild_feed_includes_processed_stories(council_feed, feed_cfg):
    rebuild_feed(council_feed, feed_cfg)
    content = (council_feed / "rss.xml").read_text()
    assert "New Housing Plan Approved" in content
    assert "Budget Amendment Passes" in content

def test_rebuild_feed_skips_ignored(council_feed, feed_cfg):
    rebuild_feed(council_feed, feed_cfg)
    content = (council_feed / "rss.xml").read_text()
    assert "Should Not Appear" not in content

def test_rebuild_feed_links_to_youtube(council_feed, feed_cfg):
    rebuild_feed(council_feed, feed_cfg)
    content = (council_feed / "rss.xml").read_text()
    assert "youtu.be/VALID1234567" in content

def test_rebuild_feed_includes_timestamp(council_feed, feed_cfg):
    rebuild_feed(council_feed, feed_cfg)
    content = (council_feed / "rss.xml").read_text()
    # First story has start_seconds=120
    assert "?t=120" in content


# ---------------------------------------------------------------------------
# rebuild_meta_feed
# ---------------------------------------------------------------------------

@pytest.fixture
def multi_council_archive(tmp_path, monkeypatch):
    """Patch STORAGE_ROOT to a tmp archive with two council directories."""
    import TubeNews
    monkeypatch.setattr(TubeNews, 'STORAGE_ROOT', tmp_path)

    for council, vid, date in [
        ("alpha_council", "ALPHA234567", "2026-02-01"),
        ("beta_council",  "BETA2345678", "2026-01-20"),
    ]:
        feed_dir = tmp_path / council
        m = _make_meeting(feed_dir, date, vid, f"{council} meeting")
        _make_story_file(m, "01_Story.md", f"Story from {council}",
                         "CITY, Calif. — February 1, 2026", "Content here.", 90)

    return tmp_path


def test_rebuild_meta_feed_creates_rss(multi_council_archive):
    rebuild_meta_feed()
    assert (multi_council_archive / "rss.xml").exists()

def test_rebuild_meta_feed_includes_all_councils(multi_council_archive):
    rebuild_meta_feed()
    content = (multi_council_archive / "rss.xml").read_text()
    assert "alpha council" in content.lower()
    assert "beta council" in content.lower()

def test_rebuild_meta_feed_base_url(multi_council_archive):
    rebuild_meta_feed(base_url="https://example.com/rss.xml")
    content = (multi_council_archive / "rss.xml").read_text()
    assert "example.com" in content

def test_rebuild_meta_feed_no_base_url_omits_self_link(multi_council_archive):
    rebuild_meta_feed(base_url="")
    content = (multi_council_archive / "rss.xml").read_text()
    assert "localhost" not in content
