"""Unit tests for TubeNews.py — run with: pytest tests/ -v"""
import json
import re
import sys
import time
from pathlib import Path

import pytest

# Make the project root importable when running pytest from any directory.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from TubeNews import (
    slugify,
    parse_story_file,
    rebuild_feed,
    rebuild_meta_feed,
)


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
# JSON story extraction regex (mirrors the logic in call_gemini_api)
# ---------------------------------------------------------------------------

STORY_REGEX = re.compile(r"\[\s*{.*}\s*\]", re.DOTALL)

def test_json_extraction_clean():
    raw = '[{"title": "Test Story", "content": "Body text"}]'
    match = STORY_REGEX.search(raw)
    assert match is not None
    stories = json.loads(match.group(0))
    assert stories[0]["title"] == "Test Story"

def test_json_extraction_prose_wrapped():
    raw = 'Here are the stories:\n[{"title": "Test", "content": "Body"}]\nEnd of report.'
    match = STORY_REGEX.search(raw)
    assert match is not None
    stories = json.loads(match.group(0))
    assert len(stories) == 1
    assert stories[0]["title"] == "Test"

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
# parse_story_file
# ---------------------------------------------------------------------------

def test_parse_story_file_basic(tmp_path):
    story = tmp_path / "01_Test_Story.md"
    story.write_text(
        "# My Headline\n"
        "*TESTVILLE, Calif. — January 1, 2026*\n\n"
        "Body text here.\n\n"
        "---\n"
        "**Segment Start:** 120s\n",
        encoding="utf-8",
    )
    result = parse_story_file(story)
    assert result["title"] == "My Headline"
    assert result["dateline"] == "TESTVILLE, Calif. — January 1, 2026"
    assert result["start_seconds"] == 120
    assert "Body text here." in result["body_html"]

def test_parse_story_file_missing_timestamp(tmp_path):
    story = tmp_path / "01_No_Time.md"
    story.write_text(
        "# Title\n*Dateline*\n\nBody.\n",
        encoding="utf-8",
    )
    result = parse_story_file(story)
    assert result["start_seconds"] == 0

def test_parse_story_file_content_hash_stable(tmp_path):
    story = tmp_path / "01_Hash.md"
    content = "# Title\n*Dateline*\n\nBody.\n\n---\n**Segment Start:** 0s\n"
    story.write_text(content, encoding="utf-8")
    r1 = parse_story_file(story)
    r2 = parse_story_file(story)
    assert r1["content_hash"] == r2["content_hash"]


# ---------------------------------------------------------------------------
# Fixtures shared by rebuild_feed and rebuild_meta_feed tests
# ---------------------------------------------------------------------------

def _write_story(meeting_dir: Path, filename: str, title: str, dateline: str,
                 content: str, start_seconds: int = 60) -> Path:
    path = meeting_dir / filename
    path.write_text(
        f"# {title}\n"
        f"*{dateline}*\n\n"
        f"{content}\n\n"
        f"---\n"
        f"**Segment Start:** {start_seconds}s\n",
        encoding="utf-8",
    )
    return path


def _make_meeting(feed_dir: Path, date_prefix: str, video_id: str,
                  title: str, status: str = "processed") -> Path:
    meeting_dir = feed_dir / f"{date_prefix}_{video_id}"
    meeting_dir.mkdir(parents=True, exist_ok=True)
    metadata = {
        "video_id": video_id,
        "video_title": title,
        "status": status,
        "processed_at": int(time.time()),
    }
    (meeting_dir / "metadata.json").write_text(json.dumps(metadata))
    return meeting_dir


@pytest.fixture
def channel_feed(tmp_path):
    """A channel feed directory with one processed meeting and one ignored."""
    feed_dir = tmp_path / "test_channel"
    feed_dir.mkdir()

    # Processed meeting with two stories.
    m1 = _make_meeting(feed_dir, "2026-01-15", "VALID1234567", "Channel Meeting Jan 15")
    _write_story(m1, "01_Housing.md", "New Housing Plan Approved",
                 "TESTVILLE, Calif. — January 15, 2026", "Approved 50 units.", 120)
    _write_story(m1, "02_Budget.md", "Budget Amendment Passes",
                 "TESTVILLE, Calif. — January 15, 2026", "A $500k amendment passed.", 300)

    # Ignored (too old) meeting — stories must NOT appear in the feed.
    m2 = _make_meeting(feed_dir, "2000-01-01", "OLD01234567", "Old Meeting",
                       status="ignored_too_old")
    _write_story(m2, "01_Old.md", "Should Not Appear",
                 "TESTVILLE, Calif. — January 1, 2000", "Old content.", 0)

    return feed_dir


@pytest.fixture
def feed_cfg():
    return {
        "channel_name": "Test Channel",
        "channel_id": "UCtest1234567890",
        "focus": "housing and zoning",
    }


# ---------------------------------------------------------------------------
# rebuild_feed
# ---------------------------------------------------------------------------

def test_rebuild_feed_creates_rss(channel_feed, feed_cfg):
    rebuild_feed(channel_feed, feed_cfg)
    assert (channel_feed / "rss.xml").exists()

def test_rebuild_feed_includes_processed_stories(channel_feed, feed_cfg):
    rebuild_feed(channel_feed, feed_cfg)
    content = (channel_feed / "rss.xml").read_text()
    assert "New Housing Plan Approved" in content
    assert "Budget Amendment Passes" in content

def test_rebuild_feed_skips_ignored(channel_feed, feed_cfg):
    rebuild_feed(channel_feed, feed_cfg)
    content = (channel_feed / "rss.xml").read_text()
    assert "Should Not Appear" not in content

def test_rebuild_feed_links_to_youtube(channel_feed, feed_cfg):
    rebuild_feed(channel_feed, feed_cfg)
    content = (channel_feed / "rss.xml").read_text()
    assert "youtu.be/VALID1234567" in content

def test_rebuild_feed_includes_timestamp(channel_feed, feed_cfg):
    rebuild_feed(channel_feed, feed_cfg)
    content = (channel_feed / "rss.xml").read_text()
    # First story has start_seconds=120.
    assert "?t=120" in content


# ---------------------------------------------------------------------------
# rebuild_meta_feed
# ---------------------------------------------------------------------------

@pytest.fixture
def multi_channel_archive(tmp_path, monkeypatch):
    """Patch STORAGE_ROOT to a tmp archive with two channel directories."""
    import TubeNews
    monkeypatch.setattr(TubeNews, "STORAGE_ROOT", tmp_path)

    for channel_slug, vid, date in [
        ("alpha_channel", "ALPHA234567", "2026-02-01"),
        ("beta_channel",  "BETA2345678", "2026-01-20"),
    ]:
        feed_dir = tmp_path / channel_slug
        m = _make_meeting(feed_dir, date, vid, f"{channel_slug} meeting")
        _write_story(m, "01_Story.md", f"Story from {channel_slug}",
                     "CITY, Calif. — February 1, 2026", "Content here.", 90)

    return tmp_path


def test_rebuild_meta_feed_creates_rss(multi_channel_archive):
    rebuild_meta_feed()
    assert (multi_channel_archive / "rss.xml").exists()

def test_rebuild_meta_feed_includes_all_channels(multi_channel_archive):
    rebuild_meta_feed()
    content = (multi_channel_archive / "rss.xml").read_text()
    assert "alpha channel" in content.lower()
    assert "beta channel" in content.lower()

def test_rebuild_meta_feed_base_url(multi_channel_archive):
    rebuild_meta_feed(base_url="https://example.com/rss.xml")
    content = (multi_channel_archive / "rss.xml").read_text()
    assert "example.com" in content

def test_rebuild_meta_feed_no_base_url_omits_self_link(multi_channel_archive):
    rebuild_meta_feed(base_url="")
    content = (multi_channel_archive / "rss.xml").read_text()
    assert "localhost" not in content
