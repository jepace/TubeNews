"""Unit tests for TubeNews.py — run with: pytest tests/ -v"""
import json
import re
import sys
import time
from pathlib import Path

import pytest

# Make the project root importable when running pytest from any directory.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from datetime import datetime, timedelta

from TubeNews import (
    slugify,
    parse_story_file,
    process_feed,
    rebuild_feed,
    rebuild_meta_feed,
    rebuild_user_feed,
    rebuild_user_blog,
    write_story_files,
    mark_video_as_backlog,
    _relative_date_to_iso,
    _parse_channel_page_metadata,
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


# ---------------------------------------------------------------------------
# _relative_date_to_iso
# ---------------------------------------------------------------------------

def test_relative_date_days_ago():
    expected = (datetime.now() - timedelta(days=5)).strftime("%Y-%m-%d")
    assert _relative_date_to_iso("5 days ago") == expected

def test_relative_date_singular_day():
    expected = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
    assert _relative_date_to_iso("1 day ago") == expected

def test_relative_date_weeks_ago():
    expected = (datetime.now() - timedelta(days=14)).strftime("%Y-%m-%d")
    assert _relative_date_to_iso("2 weeks ago") == expected

def test_relative_date_months_ago():
    expected = (datetime.now() - timedelta(days=60)).strftime("%Y-%m-%d")
    assert _relative_date_to_iso("2 months ago") == expected

def test_relative_date_hours_ago_returns_today():
    # Hours map to 0 days, so result should be today.
    expected = datetime.now().strftime("%Y-%m-%d")
    assert _relative_date_to_iso("3 hours ago") == expected

def test_relative_date_streamed_live_exact():
    assert _relative_date_to_iso("Streamed live on Feb 24, 2026") == "2026-02-24"

def test_relative_date_exact_month_day_year():
    assert _relative_date_to_iso("Mar 14, 2026") == "2026-03-14"

def test_relative_date_unknown_returns_today():
    expected = datetime.now().strftime("%Y-%m-%d")
    assert _relative_date_to_iso("some unrecognised format xyz") == expected

def test_relative_date_empty_string_returns_today():
    expected = datetime.now().strftime("%Y-%m-%d")
    assert _relative_date_to_iso("") == expected


# ---------------------------------------------------------------------------
# _parse_channel_page_metadata
# ---------------------------------------------------------------------------

def _make_yt_html(video_obj: dict) -> str:
    """Wrap a video renderer dict in minimal ytInitialData HTML."""
    data = {"tabs": [video_obj]}
    blob = json.dumps(data)
    return f"var ytInitialData = {blob};</script>"


def test_parse_channel_page_metadata_extracts_video():
    video = {
        "videoId": "abc123xyz",
        "title": {"runs": [{"text": "Test Council Meeting"}]},
        "publishedTimeText": {"simpleText": "5 days ago"},
        "thumbnailOverlays": [],
    }
    result = _parse_channel_page_metadata(_make_yt_html(video))
    assert "abc123xyz" in result
    assert result["abc123xyz"]["title"] == "Test Council Meeting"
    assert result["abc123xyz"]["is_live"] is False

def test_parse_channel_page_metadata_no_yt_initial_data():
    result = _parse_channel_page_metadata("<html><body>No data here</body></html>")
    assert result == {}

def test_parse_channel_page_metadata_invalid_json():
    result = _parse_channel_page_metadata("var ytInitialData = {NOT VALID JSON};</script>")
    assert result == {}

def test_parse_channel_page_metadata_detects_live():
    video = {
        "videoId": "live123xyz",
        "title": {"runs": [{"text": "Live Meeting"}]},
        "publishedTimeText": {"simpleText": "1 minute ago"},
        "thumbnailOverlays": [
            {"thumbnailOverlayTimeStatusRenderer": {"style": "LIVE"}}
        ],
    }
    result = _parse_channel_page_metadata(_make_yt_html(video))
    assert result["live123xyz"]["is_live"] is True

def test_parse_channel_page_metadata_detects_upcoming():
    video = {
        "videoId": "soon123xyz",
        "title": {"runs": [{"text": "Upcoming Meeting"}]},
        "publishedTimeText": {"simpleText": "1 hour ago"},
        "thumbnailOverlays": [
            {"thumbnailOverlayTimeStatusRenderer": {"style": "UPCOMING"}}
        ],
    }
    result = _parse_channel_page_metadata(_make_yt_html(video))
    assert result["soon123xyz"]["is_live"] is True

def test_parse_channel_page_metadata_no_duplicate_ids():
    # Same videoId appearing twice in the walk should only be recorded once.
    video = {
        "videoId": "dup123xyzz",
        "title": {"runs": [{"text": "Dupe"}]},
        "publishedTimeText": {"simpleText": "1 day ago"},
        "thumbnailOverlays": [],
        "nested": {
            "videoId": "dup123xyzz",
            "title": {"runs": [{"text": "Dupe Again"}]},
            "thumbnailOverlays": [],
        },
    }
    result = _parse_channel_page_metadata(_make_yt_html(video))
    assert len([k for k in result if k == "dup123xyzz"]) == 1


# ---------------------------------------------------------------------------
# write_story_files
# ---------------------------------------------------------------------------

def _story(title="Story Title", dateline="CITY, CA — Jan 1, 2026",
           content="Body text.", start=60):
    return {"title": title, "dateline": dateline, "content": content,
            "start_time_seconds": start}


def test_write_story_files_creates_file(tmp_path):
    write_story_files([_story()], tmp_path)
    files = list(tmp_path.glob("[0-9]*.md"))
    assert len(files) == 1

def test_write_story_files_numbered_prefix(tmp_path):
    write_story_files([_story("First"), _story("Second")], tmp_path)
    names = sorted(f.name for f in tmp_path.glob("[0-9]*.md"))
    assert names[0].startswith("01_")
    assert names[1].startswith("02_")

def test_write_story_files_content(tmp_path):
    write_story_files([_story(title="Council Approves Budget", content="The city approved.")], tmp_path)
    text = next(tmp_path.glob("[0-9]*.md")).read_text()
    assert "# Council Approves Budget" in text
    assert "The city approved." in text

def test_write_story_files_segment_start(tmp_path):
    write_story_files([_story(start=300)], tmp_path)
    text = next(tmp_path.glob("[0-9]*.md")).read_text()
    assert "**Segment Start:** 300s" in text

def test_write_story_files_source_link_with_video_id(tmp_path):
    write_story_files([_story(start=90)], tmp_path, video_id="TestVid1234")
    text = next(tmp_path.glob("[0-9]*.md")).read_text()
    assert "youtu.be/TestVid1234?t=90" in text

def test_write_story_files_no_source_link_without_video_id(tmp_path):
    write_story_files([_story()], tmp_path)
    text = next(tmp_path.glob("[0-9]*.md")).read_text()
    assert "youtu.be" not in text

def test_write_story_files_fallback_dateline(tmp_path):
    story = {"title": "No Dateline", "content": "Body.", "start_time_seconds": 0}
    write_story_files([story], tmp_path)
    text = next(tmp_path.glob("[0-9]*.md")).read_text()
    assert "Local News" in text

def test_write_story_files_clears_stale_files(tmp_path):
    stale = tmp_path / "01_Old_Story.md"
    stale.write_text("stale content")
    write_story_files([_story("New Story")], tmp_path)
    assert not stale.exists()
    assert len(list(tmp_path.glob("[0-9]*.md"))) == 1


# ---------------------------------------------------------------------------
# mark_video_as_backlog
# ---------------------------------------------------------------------------

def test_mark_video_as_backlog_creates_stub_dir(tmp_path):
    mark_video_as_backlog(tmp_path, "Abc123VidXX")
    assert (tmp_path / "2000-01-01_Abc123VidXX").is_dir()

def test_mark_video_as_backlog_metadata_content(tmp_path):
    mark_video_as_backlog(tmp_path, "Abc123VidXX")
    meta = json.loads((tmp_path / "2000-01-01_Abc123VidXX" / "metadata.json").read_text())
    assert meta["video_id"] == "Abc123VidXX"
    assert meta["status"] == "ignored_too_old"

def test_mark_video_as_backlog_idempotent(tmp_path):
    mark_video_as_backlog(tmp_path, "Abc123VidXX")
    mark_video_as_backlog(tmp_path, "Abc123VidXX")  # should not raise
    assert (tmp_path / "2000-01-01_Abc123VidXX").is_dir()


# ---------------------------------------------------------------------------
# rebuild_user_feed
# ---------------------------------------------------------------------------

def _setup_channel(archive_root: Path, channel_slug: str, channel_id: str,
                   channel_name: str, story_count: int = 1) -> Path:
    """Create a channel directory with channel.json, a meeting, and stories."""
    channel_dir = archive_root / channel_slug
    meeting_dir = _make_meeting(channel_dir, "2026-02-01", f"VID{channel_slug[:8].upper()}", f"{channel_name} Meeting")
    for i in range(story_count):
        _write_story(
            meeting_dir, f"0{i+1}_Story.md",
            f"Story {i+1} from {channel_name}",
            f"CITY — Feb 1, 2026", f"Content {i+1}.", 60 * (i + 1),
        )
    (channel_dir / "channel.json").write_text(
        json.dumps({"channel_id": channel_id, "channel_name": channel_name})
    )
    return channel_dir


@pytest.fixture
def user_archive(tmp_path, monkeypatch):
    """Archive with two channels; monkeypatches STORAGE_ROOT."""
    import TubeNews
    monkeypatch.setattr(TubeNews, "STORAGE_ROOT", tmp_path)
    _setup_channel(tmp_path, "alpha_city", "UC_ALPHA_ID", "Alpha City Council")
    _setup_channel(tmp_path, "beta_city",  "UC_BETA__ID", "Beta City Council")
    return tmp_path


def test_rebuild_user_feed_creates_rss(user_archive):
    user = {"name": "Jane Doe", "channel_ids": ["UC_ALPHA_ID"]}
    rebuild_user_feed(user)
    assert (user_archive / "users" / "Jane_Doe" / "rss.xml").exists()

def test_rebuild_user_feed_includes_subscribed_channel(user_archive):
    user = {"name": "Jane Doe", "channel_ids": ["UC_ALPHA_ID"]}
    rebuild_user_feed(user)
    content = (user_archive / "users" / "Jane_Doe" / "rss.xml").read_text()
    assert "Alpha City Council" in content

def test_rebuild_user_feed_excludes_unsubscribed_channel(user_archive):
    user = {"name": "Jane Doe", "channel_ids": ["UC_ALPHA_ID"]}
    rebuild_user_feed(user)
    content = (user_archive / "users" / "Jane_Doe" / "rss.xml").read_text()
    assert "Beta City Council" not in content

def test_rebuild_user_feed_no_subscriptions_empty_feed(user_archive):
    user = {"name": "No Subs", "channel_ids": []}
    rebuild_user_feed(user)
    content = (user_archive / "users" / "No_Subs" / "rss.xml").read_text()
    assert "Alpha City Council" not in content
    assert "Beta City Council" not in content

def test_rebuild_user_feed_multiple_channels(user_archive):
    user = {"name": "Both", "channel_ids": ["UC_ALPHA_ID", "UC_BETA__ID"]}
    rebuild_user_feed(user)
    content = (user_archive / "users" / "Both" / "rss.xml").read_text()
    assert "Alpha City Council" in content
    assert "Beta City Council" in content


# ---------------------------------------------------------------------------
# rebuild_user_blog
# ---------------------------------------------------------------------------

def test_rebuild_user_blog_creates_html(user_archive):
    user = {"name": "Jane Doe", "channel_ids": ["UC_ALPHA_ID"], "feed_token": "test-token-1"}
    rebuild_user_blog(user)
    assert (user_archive / "users" / "Jane_Doe" / "index.html").exists()

def test_rebuild_user_blog_includes_subscribed_stories(user_archive):
    user = {"name": "Jane Doe", "channel_ids": ["UC_ALPHA_ID"], "feed_token": "test-token-1"}
    rebuild_user_blog(user)
    content = (user_archive / "users" / "Jane_Doe" / "index.html").read_text()
    assert "Alpha City Council" in content

def test_rebuild_user_blog_excludes_unsubscribed_stories(user_archive):
    user = {"name": "Jane Doe", "channel_ids": ["UC_ALPHA_ID"], "feed_token": "test-token-1"}
    rebuild_user_blog(user)
    content = (user_archive / "users" / "Jane_Doe" / "index.html").read_text()
    assert "Beta City Council" not in content

def test_rebuild_user_blog_date_filter(tmp_path, monkeypatch):
    """Stories older than blog_days should be omitted."""
    import TubeNews
    monkeypatch.setattr(TubeNews, "STORAGE_ROOT", tmp_path)

    channel_dir = tmp_path / "old_channel"
    meeting_dir = _make_meeting(channel_dir, "2020-01-01", "VIDold12345", "Old Meeting")
    # Override processed_at to a timestamp well in the past.
    old_meta = {
        "video_id": "VIDold12345",
        "video_title": "Old Meeting",
        "status": "processed",
        "processed_at": int(time.time()) - (200 * 86400),  # 200 days ago
    }
    (meeting_dir / "metadata.json").write_text(json.dumps(old_meta))
    _write_story(meeting_dir, "01_Old.md", "Very Old Story",
                 "CITY — Jan 1, 2020", "Old content.", 0)
    (channel_dir / "channel.json").write_text(
        json.dumps({"channel_id": "UC_OLD_ID", "channel_name": "Old Channel"})
    )

    user = {"name": "Test User", "channel_ids": ["UC_OLD_ID"], "feed_token": "test-token-2"}
    rebuild_user_blog(user, blog_days=90)  # only 90 days; story is 200 days old
    content = (tmp_path / "users" / "Test_User" / "index.html").read_text()
    assert "Very Old Story" not in content


# ---------------------------------------------------------------------------
# process_feed — early-return tuple regression (Bug: 2-tuple vs 3-tuple)
# ---------------------------------------------------------------------------

def test_process_feed_empty_videos_returns_three_tuple(tmp_path, monkeypatch):
    """Regression: when discover_videos returns [], process_feed must return a 3-tuple.

    Previously returned (content_changed, ai_rate_limited) — a 2-tuple — which caused
    a ValueError at the call site in _run_feed, crashing main() before rebuild_user_feed
    could run and leaving all personal RSS feeds stale.
    """
    import TubeNews
    monkeypatch.setattr(TubeNews, "STORAGE_ROOT", tmp_path)
    monkeypatch.setattr(TubeNews, "discover_videos", lambda *a, **kw: [])

    feed = {"channel_id": "UCtest1234567890", "channel_name": "Test Channel", "focus": "test"}
    content_changed, ai_rate_limited, stories_written = process_feed(feed, None, {}, None)
    assert stories_written == 0
    assert not ai_rate_limited


def test_process_feed_empty_videos_content_changed_when_no_rss(tmp_path, monkeypatch):
    """content_changed is True on empty discover when rss.xml doesn't exist yet."""
    import TubeNews
    monkeypatch.setattr(TubeNews, "STORAGE_ROOT", tmp_path)
    monkeypatch.setattr(TubeNews, "discover_videos", lambda *a, **kw: [])

    feed = {"channel_id": "UCtest1234567890", "channel_name": "Test Channel", "focus": "test"}
    content_changed, _, _ = process_feed(feed, None, {}, None)
    assert content_changed  # no rss.xml yet → content_changed starts True


def test_process_feed_empty_videos_no_content_changed_when_rss_exists(tmp_path, monkeypatch):
    """content_changed is False on empty discover when rss.xml already exists."""
    import TubeNews
    monkeypatch.setattr(TubeNews, "STORAGE_ROOT", tmp_path)
    monkeypatch.setattr(TubeNews, "discover_videos", lambda *a, **kw: [])

    channel_dir = tmp_path / "Test_Channel"
    channel_dir.mkdir()
    (channel_dir / "rss.xml").write_text("<rss/>")

    feed = {"channel_id": "UCtest1234567890", "channel_name": "Test Channel", "focus": "test"}
    content_changed, _, _ = process_feed(feed, None, {}, None)
    assert not content_changed
