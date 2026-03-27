"""Unit tests for TubeNews.py — run with: pytest tests/ -v"""
import json
import os
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
    rebuild_aggregate_feed,
    rebuild_user_feed,
    rebuild_user_blog,
    build_user_feed_xml,
    write_story_files,
    _relative_date_to_iso,
    _parse_channel_page_metadata,
    _resolve_early_config,
    _acquire_lock,
    _release_lock,
    _story_matches_focus,
    _check_supadata_quota,
    _fmt_no_leading_zeros,
    fetch_transcript,
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
# _fmt_no_leading_zeros
# ---------------------------------------------------------------------------

def test_fmt_no_leading_zeros_single_digit_day():
    """A day < 10 must have its leading zero stripped."""
    dt = datetime(2026, 1, 5, 9, 30)
    result = _fmt_no_leading_zeros(dt, "%B %d, %Y")
    assert result == "January 5, 2026"


def test_fmt_no_leading_zeros_double_digit_day_unchanged():
    """A day >= 10 must not be altered."""
    dt = datetime(2026, 3, 14, 9, 30)
    result = _fmt_no_leading_zeros(dt, "%B %d, %Y")
    assert result == "March 14, 2026"


def test_fmt_no_leading_zeros_single_digit_hour():
    """A 12-hour clock hour < 10 must have its leading zero stripped."""
    dt = datetime(2026, 1, 5, 9, 30)
    result = _fmt_no_leading_zeros(dt, "%B %d, %Y at %I:%M %p")
    assert result == "January 5, 2026 at 9:30 AM"


def test_fmt_no_leading_zeros_double_digit_hour_unchanged():
    """A 12-hour clock hour >= 10 must not be altered."""
    dt = datetime(2026, 1, 5, 10, 30)
    result = _fmt_no_leading_zeros(dt, "%I:%M %p")
    assert result == "10:30 AM"


def test_fmt_no_leading_zeros_noon():
    """Noon (12:00 PM) must not be mangled."""
    dt = datetime(2026, 3, 14, 12, 0)
    result = _fmt_no_leading_zeros(dt, "%I:%M %p")
    assert result == "12:00 PM"


def test_fmt_no_leading_zeros_full_timestamp():
    """Full date+time string matches the docstring example exactly."""
    dt = datetime(2026, 1, 5, 9, 30)
    result = _fmt_no_leading_zeros(dt, "%B %d, %Y at %I:%M %p")
    assert result == "January 5, 2026 at 9:30 AM"


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
# Fixtures shared by rebuild_feed and rebuild_aggregate_feed tests
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
# rebuild_aggregate_feed
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


def test_rebuild_aggregate_feed_creates_rss(multi_channel_archive):
    rebuild_aggregate_feed()
    assert (multi_channel_archive / "rss.xml").exists()

def test_rebuild_aggregate_feed_includes_all_channels(multi_channel_archive):
    rebuild_aggregate_feed()
    content = (multi_channel_archive / "rss.xml").read_text()
    assert "alpha channel" in content.lower()
    assert "beta channel" in content.lower()

def test_rebuild_aggregate_feed_base_url(multi_channel_archive):
    rebuild_aggregate_feed(base_url="https://example.com/rss.xml")
    content = (multi_channel_archive / "rss.xml").read_text()
    assert "example.com" in content

def test_rebuild_aggregate_feed_no_base_url_omits_self_link(multi_channel_archive):
    rebuild_aggregate_feed(base_url="")
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


def test_parse_channel_page_metadata_simpletext_title():
    """YouTube sometimes uses simpleText instead of runs for the title field."""
    video = {
        "videoId": "simple1xyzz",
        "title": {"simpleText": "03 16 26 Joint CC & SA Meeting"},
        "publishedTimeText": {"simpleText": "4 days ago"},
        "thumbnailOverlays": [],
    }
    result = _parse_channel_page_metadata(_make_yt_html(video))
    assert result["simple1xyzz"]["title"] == "03 16 26 Joint CC & SA Meeting"

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
# New-feed auto-ignore (ignored_too_old stubs written by process_feed)
# ---------------------------------------------------------------------------

def test_new_feed_marks_older_videos_ignored_too_old(tmp_path, monkeypatch):
    """On a new feed's first run, all videos except the most recent get
    ignored_too_old stubs so the first run doesn't process months of old meetings."""
    import TubeNews
    monkeypatch.setattr(TubeNews, "STORAGE_ROOT", tmp_path)

    yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
    videos = [
        {"id": "VID_NEWEST_1", "title": "Meeting 1", "date": yesterday, "is_live": False},
        {"id": "VID_OLDER_2", "title": "Meeting 2", "date": yesterday, "is_live": False},
        {"id": "VID_OLDEST_3", "title": "Meeting 3", "date": yesterday, "is_live": False},
    ]
    monkeypatch.setattr(TubeNews, "discover_videos", lambda *a, **kw: videos)
    monkeypatch.setattr(TubeNews, "process_video", lambda **kw: ("skipped", 0))

    feed = {"channel_id": "UC_TEST_ID", "channel_name": "Test Channel", "focus": "test"}
    process_feed(feed, None, {}, None)

    channel_dir = tmp_path / "Test_Channel"
    # All videos except index 0 must have ignored_too_old stubs.
    for vid_id in ("VID_OLDER_2", "VID_OLDEST_3"):
        stub_dir = channel_dir / f"2000-01-01_{vid_id}"
        assert stub_dir.is_dir(), f"Expected ignored_too_old stub for {vid_id}"
        meta = json.loads((stub_dir / "metadata.json").read_text())
        assert meta["status"] == "ignored_too_old"
        assert meta["video_id"] == vid_id

    # The most-recent video must NOT receive a too-old stub.
    assert not (channel_dir / "2000-01-01_VID_NEWEST_1").exists()


def test_new_feed_ignored_stubs_are_idempotent(tmp_path, monkeypatch):
    """Running process_feed twice on a new feed must not raise even though stubs already exist."""
    import TubeNews
    monkeypatch.setattr(TubeNews, "STORAGE_ROOT", tmp_path)

    yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
    videos = [
        {"id": "VID_NEWEST_1", "title": "Meeting 1", "date": yesterday, "is_live": False},
        {"id": "VID_OLDER_2", "title": "Meeting 2", "date": yesterday, "is_live": False},
    ]
    monkeypatch.setattr(TubeNews, "discover_videos", lambda *a, **kw: videos)
    monkeypatch.setattr(TubeNews, "process_video", lambda **kw: ("skipped", 0))

    feed = {"channel_id": "UC_TEST_ID", "channel_name": "Test Channel", "focus": "test"}
    process_feed(feed, None, {}, None)
    process_feed(feed, None, {}, None)  # must not raise

    assert (tmp_path / "Test_Channel" / "2000-01-01_VID_OLDER_2").is_dir()


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
    assert (user_archive / "_users" / "Jane_Doe" / "rss.xml").exists()

def test_rebuild_user_feed_includes_subscribed_channel(user_archive):
    user = {"name": "Jane Doe", "channel_ids": ["UC_ALPHA_ID"]}
    rebuild_user_feed(user)
    content = (user_archive / "_users" / "Jane_Doe" / "rss.xml").read_text()
    assert "Alpha City Council" in content

def test_rebuild_user_feed_excludes_unsubscribed_channel(user_archive):
    user = {"name": "Jane Doe", "channel_ids": ["UC_ALPHA_ID"]}
    rebuild_user_feed(user)
    content = (user_archive / "_users" / "Jane_Doe" / "rss.xml").read_text()
    assert "Beta City Council" not in content

def test_rebuild_user_feed_no_subscriptions_empty_feed(user_archive):
    user = {"name": "No Subs", "channel_ids": []}
    rebuild_user_feed(user)
    content = (user_archive / "_users" / "No_Subs" / "rss.xml").read_text()
    assert "Alpha City Council" not in content
    assert "Beta City Council" not in content

def test_rebuild_user_feed_multiple_channels(user_archive):
    user = {"name": "Both", "channel_ids": ["UC_ALPHA_ID", "UC_BETA__ID"]}
    rebuild_user_feed(user)
    content = (user_archive / "_users" / "Both" / "rss.xml").read_text()
    assert "Alpha City Council" in content
    assert "Beta City Council" in content


# ---------------------------------------------------------------------------
# rebuild_user_blog
# ---------------------------------------------------------------------------

def test_rebuild_user_blog_creates_html(user_archive):
    user = {"name": "Jane Doe", "channel_ids": ["UC_ALPHA_ID"], "feed_token": "test-token-1"}
    rebuild_user_blog(user)
    assert (user_archive / "_users" / "Jane_Doe" / "index.html").exists()

def test_rebuild_user_blog_includes_subscribed_stories(user_archive):
    user = {"name": "Jane Doe", "channel_ids": ["UC_ALPHA_ID"], "feed_token": "test-token-1"}
    rebuild_user_blog(user)
    content = (user_archive / "_users" / "Jane_Doe" / "index.html").read_text()
    assert "Alpha City Council" in content

def test_rebuild_user_blog_excludes_unsubscribed_stories(user_archive):
    user = {"name": "Jane Doe", "channel_ids": ["UC_ALPHA_ID"], "feed_token": "test-token-1"}
    rebuild_user_blog(user)
    content = (user_archive / "_users" / "Jane_Doe" / "index.html").read_text()
    assert "Beta City Council" not in content

def test_rebuild_user_blog_includes_old_stories(tmp_path, monkeypatch):
    """Stories from years ago must appear in the blog — no date filter."""
    import TubeNews
    monkeypatch.setattr(TubeNews, "STORAGE_ROOT", tmp_path)

    channel_dir = tmp_path / "old_channel"
    meeting_dir = _make_meeting(channel_dir, "2020-01-01", "VIDold12345", "Old Meeting")
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
    rebuild_user_blog(user)
    content = (tmp_path / "_users" / "Test_User" / "index.html").read_text()
    assert "Very Old Story" in content


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


# ---------------------------------------------------------------------------
# build_user_feed_xml
# ---------------------------------------------------------------------------

def test_build_user_feed_xml_returns_bytes(user_archive):
    user = {"name": "Jane Doe", "channel_ids": ["UC_ALPHA_ID"]}
    result = build_user_feed_xml(user)
    assert isinstance(result, bytes)

def test_build_user_feed_xml_is_valid_rss(user_archive):
    user = {"name": "Jane Doe", "channel_ids": ["UC_ALPHA_ID"]}
    result = build_user_feed_xml(user)
    assert b"<rss" in result
    assert b"</rss>" in result

def test_build_user_feed_xml_includes_subscribed(user_archive):
    user = {"name": "Jane Doe", "channel_ids": ["UC_ALPHA_ID"]}
    result = build_user_feed_xml(user)
    assert b"Alpha City Council" in result

def test_build_user_feed_xml_excludes_unsubscribed(user_archive):
    user = {"name": "Jane Doe", "channel_ids": ["UC_ALPHA_ID"]}
    result = build_user_feed_xml(user)
    assert b"Beta City Council" not in result

def test_build_user_feed_xml_no_subscriptions_empty_feed(user_archive):
    user = {"name": "No Subs", "channel_ids": []}
    result = build_user_feed_xml(user)
    assert b"Alpha City Council" not in result
    assert b"Beta City Council" not in result

def test_build_user_feed_xml_multiple_channels(user_archive):
    user = {"name": "Both", "channel_ids": ["UC_ALPHA_ID", "UC_BETA__ID"]}
    result = build_user_feed_xml(user)
    assert b"Alpha City Council" in result
    assert b"Beta City Council" in result

def test_build_user_feed_xml_does_not_write_to_disk(user_archive):
    """Key contract: build_user_feed_xml must never touch the filesystem."""
    user = {"name": "Jane Doe", "channel_ids": ["UC_ALPHA_ID"]}
    build_user_feed_xml(user)
    assert not (user_archive / "_users" / "Jane_Doe" / "rss.xml").exists()

def test_build_user_feed_xml_skips_ignored_too_old(tmp_path, monkeypatch):
    """Stories from ignored_too_old meetings must not appear in the feed."""
    import TubeNews
    monkeypatch.setattr(TubeNews, "STORAGE_ROOT", tmp_path)

    channel_dir = tmp_path / "test_ch"
    m_ignored = _make_meeting(channel_dir, "2000-01-01", "OLDVID12345", "Old Meeting",
                              status="ignored_too_old")
    _write_story(m_ignored, "01_Old.md", "Ghost Story", "CITY — Jan 1, 2000", "Old.", 0)
    m_recent = _make_meeting(channel_dir, "2026-01-01", "NEWVID12345", "Recent Meeting")
    _write_story(m_recent, "01_New.md", "New Story", "CITY — Jan 1, 2026", "New.", 60)
    (channel_dir / "channel.json").write_text(
        json.dumps({"channel_id": "UC_TEST_ID", "channel_name": "Test Channel"})
    )

    user = {"name": "Checker", "channel_ids": ["UC_TEST_ID"]}
    result = build_user_feed_xml(user).decode()
    assert "Ghost Story" not in result
    assert "New Story" in result

def test_build_user_feed_xml_includes_youtube_link(user_archive):
    """Each story entry must link to the YouTube video with a timestamp."""
    user = {"name": "Jane Doe", "channel_ids": ["UC_ALPHA_ID"]}
    result = build_user_feed_xml(user).decode()
    assert "youtu.be/" in result
    assert "?t=" in result

def test_rebuild_user_feed_writes_same_stories_as_build_user_feed_xml(user_archive):
    """rebuild_user_feed is a thin wrapper: the file it writes must have the same
    story content as build_user_feed_xml returns."""
    user = {"name": "Compare", "channel_ids": ["UC_ALPHA_ID"]}

    xml_bytes = build_user_feed_xml(user)
    rebuild_user_feed(user)
    written = (user_archive / "_users" / "Compare" / "rss.xml").read_bytes()

    # Both must include Alpha stories …
    assert b"Story 1 from Alpha City Council" in xml_bytes
    assert b"Story 1 from Alpha City Council" in written
    # … and exclude Beta stories.
    assert b"Beta City Council" not in xml_bytes
    assert b"Beta City Council" not in written


# ---------------------------------------------------------------------------
# Feed / blog parity — feed and blog must surface the same stories
# ---------------------------------------------------------------------------

@pytest.fixture
def parity_archive(tmp_path, monkeypatch):
    """Archive with two channels, two stories each; all meetings are recent."""
    import TubeNews
    monkeypatch.setattr(TubeNews, "STORAGE_ROOT", tmp_path)
    _setup_channel(tmp_path, "channel_a", "UC_PA_ID", "Channel A", story_count=2)
    _setup_channel(tmp_path, "channel_b", "UC_PB_ID", "Channel B", story_count=2)
    return tmp_path


def test_feed_and_blog_contain_same_story_titles(parity_archive):
    """Every story title present in the RSS feed must also appear in the blog and vice versa."""
    user = {
        "name": "Parity",
        "channel_ids": ["UC_PA_ID", "UC_PB_ID"],
        "feed_token": "parity-tok",
    }

    feed_xml = build_user_feed_xml(user).decode()
    rebuild_user_blog(user)
    blog_html = (parity_archive / "_users" / "Parity" / "index.html").read_text()

    expected_titles = [
        "Story 1 from Channel A",
        "Story 2 from Channel A",
        "Story 1 from Channel B",
        "Story 2 from Channel B",
    ]
    for title in expected_titles:
        assert title in feed_xml,  f"Feed missing story: {title!r}"
        assert title in blog_html, f"Blog missing story: {title!r}"


def test_feed_and_blog_exclude_same_unsubscribed_stories(parity_archive):
    """Stories from an unsubscribed channel must be absent from both feed and blog."""
    user = {
        "name": "Selective",
        "channel_ids": ["UC_PA_ID"],          # subscribed to A only
        "feed_token": "selective-tok",
    }

    feed_xml = build_user_feed_xml(user).decode()
    rebuild_user_blog(user)
    blog_html = (parity_archive / "_users" / "Selective" / "index.html").read_text()

    assert "Story 1 from Channel A" in feed_xml
    assert "Story 1 from Channel A" in blog_html
    assert "Story 1 from Channel B" not in feed_xml,  "Feed must exclude unsubscribed channel"
    assert "Story 1 from Channel B" not in blog_html, "Blog must exclude unsubscribed channel"


def test_feed_and_blog_empty_when_no_subscriptions(parity_archive):
    """A user with no subscriptions must get an empty feed and an empty-state blog."""
    user = {
        "name": "Empty",
        "channel_ids": [],
        "feed_token": "empty-tok",
    }

    feed_xml = build_user_feed_xml(user).decode()
    rebuild_user_blog(user)
    blog_html = (parity_archive / "_users" / "Empty" / "index.html").read_text()

    assert "Channel A" not in feed_xml
    assert "Channel B" not in feed_xml
    assert "Channel A" not in blog_html
    assert "Channel B" not in blog_html


def test_old_stories_appear_in_both_feed_and_blog(tmp_path, monkeypatch):
    """Old stories must appear in both the RSS feed and the blog — neither has a date filter."""
    import TubeNews
    monkeypatch.setattr(TubeNews, "STORAGE_ROOT", tmp_path)

    channel_dir = tmp_path / "old_ch"
    old_meta = {
        "video_id": "VIDold12345",
        "video_title": "Old Meeting",
        "status": "processed",
        "processed_at": int(time.time()) - (200 * 86400),  # 200 days ago
    }
    meeting_dir = channel_dir / "2020-01-01_VIDold12345"
    meeting_dir.mkdir(parents=True)
    (meeting_dir / "metadata.json").write_text(json.dumps(old_meta))
    _write_story(meeting_dir, "01_Old.md", "Ancient Story", "CITY — Jan 1, 2020", "Old.", 0)
    (channel_dir / "channel.json").write_text(
        json.dumps({"channel_id": "UC_OLD_ID", "channel_name": "Old Channel"})
    )

    user = {"name": "Time", "channel_ids": ["UC_OLD_ID"], "feed_token": "time-tok"}

    feed_xml = build_user_feed_xml(user).decode()
    rebuild_user_blog(user)
    blog_html = (tmp_path / "_users" / "Time" / "index.html").read_text()

    assert "Ancient Story" in feed_xml,  "Old stories must appear in RSS feed"
    assert "Ancient Story" in blog_html, "Old stories must appear in blog — no date filter"


# ---------------------------------------------------------------------------
# _resolve_early_config — content_dir and request_timeout
# ---------------------------------------------------------------------------

def test_resolve_content_dir_absolute(tmp_path):
    """An absolute content_dir is used as-is for STORAGE_ROOT."""
    custom = tmp_path / "my_content"
    cfg = tmp_path / "TubeNews.json"
    cfg.write_text(json.dumps({"content_dir": str(custom)}))
    storage_root, _ = _resolve_early_config(cfg, tmp_path)
    assert storage_root == custom


def test_resolve_content_dir_relative(tmp_path):
    """A relative content_dir is resolved against base_dir."""
    cfg = tmp_path / "TubeNews.json"
    cfg.write_text(json.dumps({"content_dir": "subdir/content"}))
    storage_root, _ = _resolve_early_config(cfg, tmp_path)
    assert storage_root == (tmp_path / "subdir" / "content").resolve()


def test_resolve_content_dir_absent_defaults_to_base_content(tmp_path):
    """When content_dir is omitted, STORAGE_ROOT defaults to base_dir/content."""
    cfg = tmp_path / "TubeNews.json"
    cfg.write_text(json.dumps({"gemini_api_key": "x"}))
    storage_root, _ = _resolve_early_config(cfg, tmp_path)
    assert storage_root == tmp_path / "content"


def test_resolve_content_dir_empty_string_defaults_to_base_content(tmp_path):
    """An explicit empty string for content_dir is treated the same as absent."""
    cfg = tmp_path / "TubeNews.json"
    cfg.write_text(json.dumps({"content_dir": ""}))
    storage_root, _ = _resolve_early_config(cfg, tmp_path)
    assert storage_root == tmp_path / "content"


def test_resolve_request_timeout_custom(tmp_path):
    """A configured request_timeout is returned as an int."""
    cfg = tmp_path / "TubeNews.json"
    cfg.write_text(json.dumps({"request_timeout": 30}))
    _, timeout = _resolve_early_config(cfg, tmp_path)
    assert timeout == 30


def test_resolve_request_timeout_absent_defaults_to_15(tmp_path):
    """When request_timeout is omitted the default of 15 is returned."""
    cfg = tmp_path / "TubeNews.json"
    cfg.write_text(json.dumps({"gemini_api_key": "x"}))
    _, timeout = _resolve_early_config(cfg, tmp_path)
    assert timeout == 15


def test_resolve_falls_back_on_missing_config_file(tmp_path):
    """If TubeNews.json does not exist both defaults are returned without raising."""
    missing = tmp_path / "no_such_file.json"
    storage_root, timeout = _resolve_early_config(missing, tmp_path)
    assert storage_root == tmp_path / "content"
    assert timeout == 15


def test_resolve_falls_back_on_invalid_json(tmp_path):
    """Corrupt JSON must not crash — defaults are returned instead."""
    cfg = tmp_path / "TubeNews.json"
    cfg.write_text("{ NOT VALID JSON }")
    storage_root, timeout = _resolve_early_config(cfg, tmp_path)
    assert storage_root == tmp_path / "content"
    assert timeout == 15


def test_resolve_request_timeout_is_int_not_string(tmp_path):
    """request_timeout must be returned as int even if stored as a JSON number."""
    cfg = tmp_path / "TubeNews.json"
    cfg.write_text(json.dumps({"request_timeout": 45}))
    _, timeout = _resolve_early_config(cfg, tmp_path)
    assert isinstance(timeout, int)


# ---------------------------------------------------------------------------
# _main_body — duplicate channel_id validation
# ---------------------------------------------------------------------------

def test_main_body_rejects_duplicate_channel_ids(tmp_path, monkeypatch, caplog):
    """_main_body must log an error and return without processing when the same
    channel_id appears more than once in the feeds list."""
    import TubeNews as _tn_local
    import argparse
    import logging

    cfg = {
        "gemini_api_key": "x",
        "gemini_model": "gemini-test",
        "supadata_api_key": "y",
        "feeds": [
            {"channel_id": "UCabc", "channel_name": "Channel A", "focus": ""},
            {"channel_id": "UCabc", "channel_name": "Channel A duplicate", "focus": ""},
        ],
    }

    config_file = tmp_path / "TubeNews.json"
    config_file.write_text(json.dumps(cfg))

    supadata_called = []

    monkeypatch.setattr(_tn_local, "CONFIG_FILE", config_file)
    monkeypatch.setattr(_tn_local, "STORAGE_ROOT", tmp_path)
    monkeypatch.setattr(
        _tn_local, "Supadata",
        lambda api_key: supadata_called.append(api_key) or object()
    )

    args = argparse.Namespace(debug=False)
    with caplog.at_level(logging.ERROR):
        _tn_local._main_body(args)

    assert supadata_called == [], "Supadata must not be instantiated on duplicate channel"
    assert any("UCabc" in r.message for r in caplog.records), \
        "Error message must name the duplicate channel_id"


# ---------------------------------------------------------------------------
# _acquire_lock / _release_lock
# ---------------------------------------------------------------------------

def test_acquire_lock_creates_file(tmp_path, monkeypatch):
    """Acquiring a free lock creates the lock file containing our PID."""
    import TubeNews
    lock = tmp_path / ".tubenews.lock"
    monkeypatch.setattr(TubeNews, "LOCK_FILE", lock)
    try:
        assert _acquire_lock()
        assert lock.exists()
        assert int(lock.read_text().strip()) == os.getpid()
    finally:
        _release_lock()


def test_acquire_lock_returns_false_when_held_by_live_process(tmp_path, monkeypatch):
    """Returns False when the lock file already contains a live PID."""
    import TubeNews
    lock = tmp_path / ".tubenews.lock"
    monkeypatch.setattr(TubeNews, "LOCK_FILE", lock)
    # Write our own PID — we are alive, so the lock is legitimately held.
    lock.write_text(str(os.getpid()))
    assert not _acquire_lock()


def test_acquire_lock_clears_stale_lock_and_succeeds(tmp_path, monkeypatch):
    """A lock file containing a dead PID is removed and the acquire succeeds."""
    import subprocess as _sp
    import TubeNews
    lock = tmp_path / ".tubenews.lock"
    monkeypatch.setattr(TubeNews, "LOCK_FILE", lock)
    # Spawn a process, wait for it to finish, then use its (now-dead) PID.
    proc = _sp.Popen([sys.executable, "-c", "pass"])
    dead_pid = proc.pid
    proc.wait()
    lock.write_text(str(dead_pid))
    try:
        assert _acquire_lock()
        assert int(lock.read_text().strip()) == os.getpid()
    finally:
        _release_lock()


def test_acquire_lock_clears_garbage_lock_and_succeeds(tmp_path, monkeypatch):
    """A lock file with non-numeric content is treated as stale."""
    import TubeNews
    lock = tmp_path / ".tubenews.lock"
    monkeypatch.setattr(TubeNews, "LOCK_FILE", lock)
    lock.write_text("not-a-pid")
    try:
        assert _acquire_lock()
    finally:
        _release_lock()


def test_release_lock_removes_file(tmp_path, monkeypatch):
    """_release_lock removes the lock file."""
    import TubeNews
    lock = tmp_path / ".tubenews.lock"
    monkeypatch.setattr(TubeNews, "LOCK_FILE", lock)
    _acquire_lock()
    _release_lock()
    assert not lock.exists()


def test_release_lock_is_noop_when_not_held(tmp_path, monkeypatch):
    """_release_lock must not raise when no lock file exists."""
    import TubeNews
    lock = tmp_path / ".tubenews.lock"
    monkeypatch.setattr(TubeNews, "LOCK_FILE", lock)
    _release_lock()  # must not raise


def test_acquire_lock_succeeds_again_after_release(tmp_path, monkeypatch):
    """After releasing, acquiring the lock must succeed a second time."""
    import TubeNews
    lock = tmp_path / ".tubenews.lock"
    monkeypatch.setattr(TubeNews, "LOCK_FILE", lock)
    assert _acquire_lock()
    _release_lock()
    assert _acquire_lock()
    _release_lock()


# ---------------------------------------------------------------------------
# _story_matches_focus
# ---------------------------------------------------------------------------

def test_focus_empty_always_matches():
    """No focus set — every story should be shown regardless of topics."""
    assert _story_matches_focus(["housing", "zoning"], [""]) is True

def test_focus_none_always_matches():
    assert _story_matches_focus(["budget"], None) is True

def test_focus_whitespace_only_always_matches():
    assert _story_matches_focus(["housing"], ["   "]) is True

def test_empty_topics_always_matches():
    """Old story with no topics must pass through unfiltered."""
    assert _story_matches_focus([], ["housing, zoning"]) is True

def test_exact_keyword_match():
    assert _story_matches_focus(["housing"], ["housing, permits"]) is True

def test_topic_substring_of_focus_keyword():
    """'housing' is a substring of focus keyword 'affordable housing'."""
    assert _story_matches_focus(["housing"], ["affordable housing, permits"]) is True

def test_focus_keyword_substring_of_topic():
    """Focus 'permit' matches topic 'permits'."""
    assert _story_matches_focus(["permits"], ["permit, budget"]) is True

def test_no_match_returns_false():
    assert _story_matches_focus(["contracts", "hr"], ["housing, zoning"]) is False

def test_any_topic_match_is_sufficient():
    """If at least one topic matches the focus, the story should be shown."""
    assert _story_matches_focus(["contracts", "budget", "zoning"], ["housing, zoning"]) is True

def test_case_insensitive_match():
    assert _story_matches_focus(["Housing"], ["housing, permits"]) is True

def test_multiple_focus_keywords_checked():
    """All focus keywords are checked, not just the first."""
    assert _story_matches_focus(["permits"], ["housing, permits, zoning"]) is True


# ---------------------------------------------------------------------------
# parse_story_file — topics field
# ---------------------------------------------------------------------------

def test_parse_story_topics_present(tmp_path):
    """Topics line is parsed into a list."""
    story = tmp_path / "01_Test.md"
    story.write_text(
        "# Title\n*Dateline*\n\nBody.\n\n---\n"
        "**Segment Start:** 60s\n"
        "**Topics:** housing, zoning, permits\n",
        encoding="utf-8",
    )
    result = parse_story_file(story)
    assert result["topics"] == ["housing", "zoning", "permits"]

def test_parse_story_topics_absent_returns_empty_list(tmp_path):
    """Old story files without a Topics line return an empty list, not an error."""
    story = tmp_path / "01_Old.md"
    story.write_text(
        "# Title\n*Dateline*\n\nBody.\n\n---\n**Segment Start:** 0s\n",
        encoding="utf-8",
    )
    result = parse_story_file(story)
    assert result["topics"] == []

def test_parse_story_topics_whitespace_stripped(tmp_path):
    """Whitespace around topic keywords is stripped."""
    story = tmp_path / "01_Test.md"
    story.write_text(
        "# Title\n*Dateline*\n\nBody.\n\n---\n"
        "**Segment Start:** 0s\n"
        "**Topics:**  housing ,  zoning  \n",
        encoding="utf-8",
    )
    result = parse_story_file(story)
    assert result["topics"] == ["housing", "zoning"]


# ---------------------------------------------------------------------------
# write_story_files — topics written to file
# ---------------------------------------------------------------------------

def test_write_story_files_includes_topics_line(tmp_path):
    """When a story has a topics list, **Topics:** must appear in the written file."""
    stories = [{
        "title": "Housing Plan Approved",
        "dateline": "GILROY, Calif. — March 22, 2026",
        "content": "The council approved the housing plan.",
        "start_time_seconds": 120,
        "topics": ["housing", "zoning"],
    }]
    write_story_files(stories, tmp_path, video_id="abc123")
    written = list(tmp_path.glob("[0-9]*.md"))
    assert len(written) == 1
    text = written[0].read_text(encoding="utf-8")
    assert "**Topics:** housing, zoning" in text

def test_write_story_files_no_topics_line_when_absent(tmp_path):
    """When topics is missing from the story dict, no **Topics:** line is written."""
    stories = [{
        "title": "Budget Update",
        "dateline": "GILROY, Calif. — March 22, 2026",
        "content": "Budget was discussed.",
        "start_time_seconds": 0,
    }]
    write_story_files(stories, tmp_path)
    written = list(tmp_path.glob("[0-9]*.md"))
    assert len(written) == 1
    text = written[0].read_text(encoding="utf-8")
    assert "**Topics:**" not in text

def test_write_story_files_empty_topics_no_line(tmp_path):
    """An empty topics list must not produce a **Topics:** line."""
    stories = [{
        "title": "Parks Update",
        "dateline": "GILROY, Calif. — March 22, 2026",
        "content": "Parks discussed.",
        "start_time_seconds": 0,
        "topics": [],
    }]
    write_story_files(stories, tmp_path)
    written = list(tmp_path.glob("[0-9]*.md"))
    text = written[0].read_text(encoding="utf-8")
    assert "**Topics:**" not in text

def test_write_story_files_user_ids_written(tmp_path):
    """When a story has _user_ids, a **Users:** line is written."""
    stories = [{
        "title": "Road Work Approved",
        "dateline": "GILROY, Calif. — March 22, 2026",
        "content": "Roads discussed.",
        "start_time_seconds": 0,
        "_user_ids": ["uuid-alice", "uuid-bob"],
    }]
    write_story_files(stories, tmp_path)
    text = list(tmp_path.glob("[0-9]*.md"))[0].read_text(encoding="utf-8")
    assert "**Users:** uuid-alice, uuid-bob" in text

def test_write_story_files_no_user_ids_no_users_line(tmp_path):
    """When _user_ids is absent, no **Users:** line is written."""
    stories = [{
        "title": "Budget Passed",
        "dateline": "GILROY, Calif. — March 22, 2026",
        "content": "Budget passed.",
        "start_time_seconds": 0,
    }]
    write_story_files(stories, tmp_path)
    text = list(tmp_path.glob("[0-9]*.md"))[0].read_text(encoding="utf-8")
    assert "**Users:**" not in text

def test_parse_story_file_user_ids_present(tmp_path):
    """**Users:** line is parsed into a list of UUIDs."""
    story = tmp_path / "01_Test.md"
    story.write_text(
        "# Title\n*Dateline*\n\nBody.\n\n---\n"
        "**Segment Start:** 60s\n"
        "**Users:** uuid-alice, uuid-bob\n",
        encoding="utf-8",
    )
    result = parse_story_file(story)
    assert result["user_ids"] == ["uuid-alice", "uuid-bob"]

def test_parse_story_file_user_ids_absent(tmp_path):
    """Old story files without a **Users:** line return an empty user_ids list."""
    story = tmp_path / "01_Old.md"
    story.write_text(
        "# Title\n*Dateline*\n\nBody.\n\n---\n**Segment Start:** 0s\n",
        encoding="utf-8",
    )
    result = parse_story_file(story)
    assert result["user_ids"] == []

def test_parse_story_file_users_line_not_in_body(tmp_path):
    """The **Users:** line must not appear in body_html."""
    story = tmp_path / "01_Test.md"
    story.write_text(
        "# Title\n*Dateline*\n\nBody.\n\n---\n"
        "**Segment Start:** 0s\n"
        "**Users:** uuid-alice\n",
        encoding="utf-8",
    )
    result = parse_story_file(story)
    assert "Users" not in result["body_html"]


def test_parse_story_file_escapes_html_in_body(tmp_path):
    """HTML special characters in the story body must be escaped (XSS prevention)."""
    story = tmp_path / "01_Xss.md"
    story.write_text(
        "# Title\n*Dateline*\n\n"
        "Council approved <script>alert(1)</script> the budget.\n\n"
        "---\n**Segment Start:** 0s\n",
        encoding="utf-8",
    )
    result = parse_story_file(story)
    assert "<script>" not in result["body_html"]
    assert "&lt;script&gt;" in result["body_html"]


# ---------------------------------------------------------------------------
# _story_matches_focus — list input (multiple focuses)
# ---------------------------------------------------------------------------

def test_focus_list_empty_list_always_matches():
    """An empty focuses list means no filter — all stories pass."""
    assert _story_matches_focus(["housing"], []) is True

def test_focus_list_with_one_empty_string_always_matches():
    assert _story_matches_focus(["housing"], [""]) is True

def test_focus_list_matches_any_focus():
    """Story passes if it matches ANY element of the focuses list."""
    assert _story_matches_focus(["roads"], ["housing, zoning", "transportation, roads"]) is True

def test_focus_list_no_match_in_any_focus():
    assert _story_matches_focus(["contracts"], ["housing, zoning", "transportation"]) is False

def test_focus_list_empty_topics_always_matches():
    """Old stories with no topics pass through even with a multi-focus list."""
    assert _story_matches_focus([], ["housing", "transportation"]) is True

# ---------------------------------------------------------------------------
# write_story_files — append mode (clear_existing=False, start_index)
# ---------------------------------------------------------------------------

def test_write_story_files_append_does_not_clear_existing(tmp_path):
    """clear_existing=False must leave pre-existing story files intact."""
    # Write an existing story directly
    (tmp_path / "01_Old_Story.md").write_text("# Old Story\n*Dateline*\n\nBody.\n")

    new_stories = [{"title": "New Story", "dateline": "CITY — 2026", "content": "New.", "start_time_seconds": 0}]
    write_story_files(new_stories, tmp_path, clear_existing=False, start_index=2)

    assert (tmp_path / "01_Old_Story.md").exists(), "existing file must survive"
    new_files = [f for f in tmp_path.glob("[0-9]*.md") if f.name != "01_Old_Story.md"]
    assert len(new_files) == 1
    assert new_files[0].name.startswith("02_")

def test_write_story_files_clear_existing_removes_old(tmp_path):
    """clear_existing=True (default) must delete stale story files."""
    (tmp_path / "01_Stale.md").write_text("# Stale\n*Dateline*\n\nBody.\n")

    new_stories = [{"title": "Fresh Story", "dateline": "CITY — 2026", "content": "Fresh.", "start_time_seconds": 0}]
    write_story_files(new_stories, tmp_path)  # clear_existing=True by default

    assert not (tmp_path / "01_Stale.md").exists()
    assert len(list(tmp_path.glob("[0-9]*.md"))) == 1

def test_write_story_files_start_index(tmp_path):
    """start_index controls the numbering prefix of new files."""
    stories = [{"title": "Story", "dateline": "CITY — 2026", "content": "Body.", "start_time_seconds": 0}]
    write_story_files(stories, tmp_path, clear_existing=False, start_index=5)
    files = list(tmp_path.glob("[0-9]*.md"))
    assert len(files) == 1
    assert files[0].name.startswith("05_")


# ---------------------------------------------------------------------------
# _needs_processing
# ---------------------------------------------------------------------------

from TubeNews import _needs_processing, STORAGE_ROOT


def test_needs_processing_no_dir(tmp_path):
    """No archive directory → needs processing."""
    assert _needs_processing("VID123", tmp_path) is True

def test_needs_processing_no_metadata(tmp_path):
    """Dir exists with transcript but no metadata → recovery path."""
    d = tmp_path / "2026-01-01_VID123"
    d.mkdir()
    (d / "transcript.txt").write_text("transcript")
    assert _needs_processing("VID123", tmp_path) is True

def test_needs_processing_ignored_too_old(tmp_path):
    """ignored_too_old status → never reprocess."""
    d = tmp_path / "2000-01-01_VID123"
    d.mkdir()
    (d / "metadata.json").write_text(json.dumps({"status": "ignored_too_old"}))
    assert _needs_processing("VID123", tmp_path) is False

def test_needs_processing_metadata_exists(tmp_path):
    """Any metadata.json present → skip (the past is past)."""
    d = tmp_path / "2026-01-01_VID123"
    d.mkdir()
    (d / "metadata.json").write_text(json.dumps({"status": "processed", "video_id": "VID123"}))
    assert _needs_processing("VID123", tmp_path) is False


# ---------------------------------------------------------------------------
# _collect_channel_focuses
# ---------------------------------------------------------------------------

from TubeNews import _collect_channel_focuses, MAX_FOCUSES_PER_CHANNEL
import TubeNews as _tn


def _make_user_dir(users_dir, channel_id, focuses, channel_ids=None):
    import uuid as _uuid
    uid = str(_uuid.uuid4())
    d = users_dir / uid
    d.mkdir(parents=True)
    (d / "user.json").write_text(json.dumps({
        "name": "Test",
        "email": f"{uid[:8]}@example.com",
        "channel_ids": channel_ids if channel_ids is not None else [channel_id],
        "channel_focus": {channel_id: focuses},
    }))
    return d


def test_collect_channel_focuses_feed_only(tmp_path, monkeypatch):
    """Only feed_focus, no subscribers → returns [(feed_focus, [])] (unrestricted)."""
    monkeypatch.setattr(_tn, "STORAGE_ROOT", tmp_path)
    result = _collect_channel_focuses("UCxxx", "housing, zoning")
    assert result == [("housing, zoning", [])]

def test_collect_channel_focuses_user_focus_added(tmp_path, monkeypatch):
    """User focus for the channel is appended after feed_focus with the user's ID."""
    monkeypatch.setattr(_tn, "STORAGE_ROOT", tmp_path)
    users = tmp_path / "_users"
    users.mkdir()
    uid_dir = _make_user_dir(users, "UCxxx", ["transit, roads"])
    result = _collect_channel_focuses("UCxxx", "housing, zoning")
    focuses = [f for f, _ in result]
    assert focuses == ["housing, zoning", "transit, roads"]
    # Feed-level focus is unrestricted; user focus has the user's UUID
    assert result[0][1] == []
    assert uid_dir.name in result[1][1]

def test_collect_channel_focuses_user_not_subscribed_excluded(tmp_path, monkeypatch):
    """A user not subscribed to the channel must not contribute focuses."""
    monkeypatch.setattr(_tn, "STORAGE_ROOT", tmp_path)
    users = tmp_path / "_users"
    users.mkdir()
    _make_user_dir(users, "UCxxx", ["transit"], channel_ids=["UCother"])
    result = _collect_channel_focuses("UCxxx", "housing")
    assert result == [("housing", [])]

def test_collect_channel_focuses_deduplication(tmp_path, monkeypatch):
    """Same focus from two users → one entry with both user IDs."""
    monkeypatch.setattr(_tn, "STORAGE_ROOT", tmp_path)
    users = tmp_path / "_users"
    users.mkdir()
    uid1 = _make_user_dir(users, "UCxxx", ["housing, zoning"])
    uid2 = _make_user_dir(users, "UCxxx", ["housing, zoning"])
    result = _collect_channel_focuses("UCxxx", "")
    focuses = [f for f, _ in result]
    assert focuses.count("housing, zoning") == 1
    user_ids = result[0][1]
    assert uid1.name in user_ids and uid2.name in user_ids

def test_collect_channel_focuses_cap(tmp_path, monkeypatch):
    """Total focuses are capped at MAX_FOCUSES_PER_CHANNEL."""
    monkeypatch.setattr(_tn, "STORAGE_ROOT", tmp_path)
    users = tmp_path / "_users"
    users.mkdir()
    for i in range(MAX_FOCUSES_PER_CHANNEL + 3):
        _make_user_dir(users, "UCxxx", [f"focus_{i}"])
    result = _collect_channel_focuses("UCxxx", "")
    assert len(result) <= MAX_FOCUSES_PER_CHANNEL

def test_collect_channel_focuses_fallback_empty(tmp_path, monkeypatch):
    """No config and no subscribers → [("", [])]."""
    monkeypatch.setattr(_tn, "STORAGE_ROOT", tmp_path)
    result = _collect_channel_focuses("UCxxx", "")
    assert result == [("", [])]

def test_collect_channel_focuses_feed_focus_absorbs_matching_user_focus(tmp_path, monkeypatch):
    """When a user focus matches the feed-level focus it stays unrestricted."""
    monkeypatch.setattr(_tn, "STORAGE_ROOT", tmp_path)
    users = tmp_path / "_users"
    users.mkdir()
    _make_user_dir(users, "UCxxx", ["housing, zoning"])
    result = _collect_channel_focuses("UCxxx", "housing, zoning")
    assert result == [("housing, zoning", [])]  # still unrestricted

def test_collect_channel_focuses_user_ids_merged_across_users(tmp_path, monkeypatch):
    """Two users sharing a focus get their IDs merged into one entry."""
    monkeypatch.setattr(_tn, "STORAGE_ROOT", tmp_path)
    users = tmp_path / "_users"
    users.mkdir()
    uid1 = _make_user_dir(users, "UCxxx", ["roads"])
    uid2 = _make_user_dir(users, "UCxxx", ["roads"])
    result = _collect_channel_focuses("UCxxx", "")
    assert len(result) == 1
    assert set(result[0][1]) == {uid1.name, uid2.name}


# ---------------------------------------------------------------------------
# process_feed() — end-to-end with mocked external APIs
# ---------------------------------------------------------------------------

def test_process_feed_processes_new_video(tmp_path, monkeypatch):
    """process_feed must fetch a transcript, call Gemini, and write story files."""
    import TubeNews
    monkeypatch.setattr(TubeNews, "STORAGE_ROOT", tmp_path)

    yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
    monkeypatch.setattr(TubeNews, "discover_videos", lambda *a, **kw: [
        {"id": "VID_NEW_AAAA", "title": "Council Meeting", "date": yesterday, "is_live": False},
    ])
    monkeypatch.setattr(TubeNews, "fetch_transcript",
                        lambda *a, **kw: "0:00 --> The council discussed housing.")
    monkeypatch.setattr(TubeNews, "call_gemini_api", lambda *a, **kw: [
        {"title": "Housing Plan Approved", "dateline": "CITY — Mar 20, 2026",
         "content": "Body text.", "start_time_seconds": 0, "topics": ["housing"]},
    ])

    from unittest.mock import MagicMock
    feed = {"channel_id": "UCtest1234567890", "channel_name": "Test Channel", "focus": "housing"}
    content_changed, ai_rate_limited, stories_written = process_feed(
        feed, MagicMock(), {"gemini_api_key": "k", "gemini_model": "m"}, None
    )

    assert stories_written == 1
    assert content_changed
    assert not ai_rate_limited

    # Story file must exist on disk
    story_files = list((tmp_path / "Test_Channel").glob("*/[0-9]*.md"))
    assert len(story_files) == 1
    assert "Housing Plan Approved" in story_files[0].read_text()

    # metadata.json must record the video as processed
    meta_files = list((tmp_path / "Test_Channel").glob("*/metadata.json"))
    assert len(meta_files) == 1
    meta = json.loads(meta_files[0].read_text())
    assert meta["status"] == "processed"
    assert meta["video_id"] == "VID_NEW_AAAA"


def test_process_feed_skips_video_with_no_transcript(tmp_path, monkeypatch):
    """When fetch_transcript returns None, the video must be skipped and no stories written."""
    import TubeNews
    monkeypatch.setattr(TubeNews, "STORAGE_ROOT", tmp_path)

    yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
    monkeypatch.setattr(TubeNews, "discover_videos", lambda *a, **kw: [
        {"id": "VID_NO_TRANS", "title": "Council Meeting", "date": yesterday, "is_live": False},
    ])
    monkeypatch.setattr(TubeNews, "fetch_transcript", lambda *a, **kw: None)
    gemini_called = []
    monkeypatch.setattr(TubeNews, "call_gemini_api",
                        lambda *a, **kw: gemini_called.append(1) or [])

    from unittest.mock import MagicMock
    feed = {"channel_id": "UCtest1234567890", "channel_name": "Test Channel", "focus": "test"}
    _, _, stories_written = process_feed(
        feed, MagicMock(), {"gemini_api_key": "k", "gemini_model": "m"}, None
    )

    assert stories_written == 0
    assert gemini_called == [], "Gemini must not be called when transcript is unavailable"


def test_process_feed_propagates_ai_rate_limit(tmp_path, monkeypatch):
    """When Gemini returns False (429), process_feed must set ai_rate_limited=True."""
    import TubeNews
    monkeypatch.setattr(TubeNews, "STORAGE_ROOT", tmp_path)

    yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
    monkeypatch.setattr(TubeNews, "discover_videos", lambda *a, **kw: [
        {"id": "VID_RATE_LIM", "title": "Council Meeting", "date": yesterday, "is_live": False},
    ])
    monkeypatch.setattr(TubeNews, "fetch_transcript",
                        lambda *a, **kw: "0:00 --> Transcript text.")
    monkeypatch.setattr(TubeNews, "call_gemini_api", lambda *a, **kw: False)  # 429

    from unittest.mock import MagicMock
    feed = {"channel_id": "UCtest1234567890", "channel_name": "Test Channel", "focus": "test"}
    _, ai_rate_limited, stories_written = process_feed(
        feed, MagicMock(), {"gemini_api_key": "k", "gemini_model": "m"}, None
    )

    assert ai_rate_limited
    assert stories_written == 0


def test_process_feed_gemini_no_stories_writes_no_stories_metadata(tmp_path, monkeypatch):
    """When Gemini returns an empty list, metadata must be written with status 'no_stories'."""
    import TubeNews
    monkeypatch.setattr(TubeNews, "STORAGE_ROOT", tmp_path)

    yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
    monkeypatch.setattr(TubeNews, "discover_videos", lambda *a, **kw: [
        {"id": "VID_NO_STORY", "title": "Council Meeting", "date": yesterday, "is_live": False},
    ])
    monkeypatch.setattr(TubeNews, "fetch_transcript",
                        lambda *a, **kw: "0:00 --> Transcript text.")
    monkeypatch.setattr(TubeNews, "call_gemini_api", lambda *a, **kw: [])  # no stories

    from unittest.mock import MagicMock
    feed = {"channel_id": "UCtest1234567890", "channel_name": "Test Channel", "focus": "test"}
    _, _, stories_written = process_feed(
        feed, MagicMock(), {"gemini_api_key": "k", "gemini_model": "m"}, None
    )

    assert stories_written == 0
    meta_files = list((tmp_path / "Test_Channel").glob("*/metadata.json"))
    assert len(meta_files) == 1
    assert json.loads(meta_files[0].read_text())["status"] == "no_stories"


# ---------------------------------------------------------------------------
# Corrupt-file resilience
# ---------------------------------------------------------------------------

def test_rebuild_feed_skips_corrupt_story_file(tmp_path):
    """rebuild_feed must produce a valid feed even when a story .md file is corrupt."""
    feed_dir = tmp_path / "Test_Channel"
    meeting_dir = _make_meeting(feed_dir, "2026-03-01", "VID_GOOD_001", "Good Meeting")
    _write_story(meeting_dir, "01_Good_Story.md", "Good Story", "CITY — Mar 1, 2026", "Body.", 60)

    # Second meeting with a corrupt story file (binary garbage)
    meeting_dir2 = _make_meeting(feed_dir, "2026-03-02", "VID_CORRUPT_002", "Bad Meeting")
    (meeting_dir2 / "01_Corrupt.md").write_bytes(b"\xff\xfe corrupt \x00\x01")

    feed_cfg = {"channel_id": "UCtest1234567890", "channel_name": "Test Channel", "focus": "test"}
    rebuild_feed(feed_dir, feed_cfg)  # must not raise

    rss = (feed_dir / "rss.xml").read_text()
    assert "Good Story" in rss


def test_rebuild_aggregate_feed_skips_corrupt_metadata(tmp_path, monkeypatch):
    """rebuild_aggregate_feed must skip directories with corrupt metadata.json."""
    import TubeNews
    monkeypatch.setattr(TubeNews, "STORAGE_ROOT", tmp_path)

    # Good channel
    good_dir = _make_meeting(tmp_path / "good_channel", "2026-03-01", "VID_GOOD", "Good Meeting")
    _write_story(good_dir, "01_Story.md", "Good Story", "CITY — Mar 1, 2026", "Body.", 60)

    # Channel with corrupt metadata.json
    bad_meeting = tmp_path / "bad_channel" / "2026-03-01_VID_BAD"
    bad_meeting.mkdir(parents=True)
    (bad_meeting / "metadata.json").write_bytes(b"}{not valid json}")

    rebuild_aggregate_feed()  # must not raise
    rss = (tmp_path / "rss.xml").read_text()
    assert "Good Story" in rss


def test_build_user_feed_xml_skips_corrupt_channel_json(tmp_path, monkeypatch):
    """build_user_feed_xml must skip channels whose channel.json is unreadable."""
    import TubeNews
    monkeypatch.setattr(TubeNews, "STORAGE_ROOT", tmp_path)

    # Good channel
    good_dir = _setup_channel(tmp_path, "good_channel", "UC_GOOD_ID", "Good Channel")

    # Channel with corrupt channel.json (build_user_feed_xml reads it to match subscriptions)
    bad_dir = tmp_path / "bad_channel"
    bad_dir.mkdir()
    (bad_dir / "channel.json").write_bytes(b"}{not valid json}")
    meeting = bad_dir / "2026-03-01_VID_BAD"
    meeting.mkdir()
    (meeting / "metadata.json").write_text(json.dumps({
        "video_id": "VID_BAD", "video_title": "Bad", "status": "processed",
        "processed_at": int(time.time()),
    }))
    _write_story(meeting, "01_Bad.md", "Bad Story", "CITY — Mar 1, 2026", "Body.", 60)

    user = {"name": "Alice", "channel_ids": ["UC_GOOD_ID"]}
    result = build_user_feed_xml(user)  # must not raise
    assert b"Good Channel" in result
    assert b"Bad Story" not in result


def test_rebuild_user_blog_skips_corrupt_story_file(tmp_path, monkeypatch):
    """rebuild_user_blog must produce a page even when a story .md file is corrupt."""
    import TubeNews
    monkeypatch.setattr(TubeNews, "STORAGE_ROOT", tmp_path)

    channel_dir = _setup_channel(tmp_path, "alpha_city", "UC_ALPHA_ID", "Alpha City Council")

    # Add a corrupt story alongside the good one
    good_meeting = next(channel_dir.glob("*/"))
    (good_meeting / "02_Corrupt.md").write_bytes(b"\xff\xfe garbage \x00")

    user = {"name": "Alice", "channel_ids": ["UC_ALPHA_ID"], "feed_token": "test-token-xyz"}
    rebuild_user_blog(user)  # must not raise

    html = (tmp_path / "_users" / "Alice" / "index.html").read_text()
    assert "Story 1 from Alpha City Council" in html


# ---------------------------------------------------------------------------
# Supadata quota handling
# ---------------------------------------------------------------------------

def test_check_supadata_quota_no_file_proceeds(tmp_path, monkeypatch):
    """When no cached balance file exists, quota check returns ok=True."""
    import TubeNews
    monkeypatch.setattr(TubeNews, "STORAGE_ROOT", tmp_path)
    ok, balance = _check_supadata_quota({"supadata_api_key": "key"})
    assert ok is True
    assert balance is None


def test_check_supadata_quota_credits_remaining(tmp_path, monkeypatch):
    """When credits are available, quota check returns ok=True."""
    import TubeNews
    monkeypatch.setattr(TubeNews, "STORAGE_ROOT", tmp_path)
    (tmp_path / "_run_logs").mkdir()
    (tmp_path / "_run_logs" / "supadata_balance.json").write_text(json.dumps({
        "maxCredits": 1000, "usedCredits": 500, "plan": "starter",
    }))
    ok, balance = _check_supadata_quota({"supadata_api_key": "key"})
    assert ok is True
    assert balance["usedCredits"] == 500


def test_check_supadata_quota_exhausted(tmp_path, monkeypatch):
    """When usedCredits == maxCredits, quota check returns ok=False."""
    import TubeNews
    monkeypatch.setattr(TubeNews, "STORAGE_ROOT", tmp_path)
    (tmp_path / "_run_logs").mkdir()
    (tmp_path / "_run_logs" / "supadata_balance.json").write_text(json.dumps({
        "maxCredits": 1000, "usedCredits": 1000, "plan": "starter",
        "resetDate": "2026-04-01",
    }))
    ok, balance = _check_supadata_quota({"supadata_api_key": "key"})
    assert ok is False
    assert balance["resetDate"] == "2026-04-01"


def test_check_supadata_quota_over_limit(tmp_path, monkeypatch):
    """When usedCredits exceeds maxCredits, quota check returns ok=False."""
    import TubeNews
    monkeypatch.setattr(TubeNews, "STORAGE_ROOT", tmp_path)
    (tmp_path / "_run_logs").mkdir()
    (tmp_path / "_run_logs" / "supadata_balance.json").write_text(json.dumps({
        "maxCredits": 1000, "usedCredits": 1001, "plan": "starter",
    }))
    ok, _ = _check_supadata_quota({"supadata_api_key": "key"})
    assert ok is False


def test_check_supadata_quota_corrupt_file_proceeds(tmp_path, monkeypatch):
    """A corrupt balance file is treated as 'unknown' — proceed optimistically."""
    import TubeNews
    monkeypatch.setattr(TubeNews, "STORAGE_ROOT", tmp_path)
    (tmp_path / "_run_logs").mkdir()
    (tmp_path / "_run_logs" / "supadata_balance.json").write_bytes(b"\xff\xfe not json")
    ok, balance = _check_supadata_quota({"supadata_api_key": "key"})
    assert ok is True
    assert balance is None


def test_fetch_transcript_sets_quota_event_on_supadata_error(monkeypatch):
    """fetch_transcript sets transcript_rate_limit_event when SupadataError has a credit error code."""
    import threading
    import TubeNews
    from supadata import SupadataError

    mock_client = type("C", (), {
        "transcript": staticmethod(lambda **kw: (_ for _ in ()).throw(
            SupadataError(error="insufficient-credits", message="No credits", details="")
        ))
    })()

    event = threading.Event()
    result = fetch_transcript("VID123", mock_client, transcript_rate_limit_event=event)
    assert result is None
    assert event.is_set()


def test_fetch_transcript_sets_quota_event_on_http_402(monkeypatch):
    """fetch_transcript sets transcript_rate_limit_event on HTTP 402 HTTPError."""
    import threading
    import requests as _requests
    from unittest.mock import MagicMock

    mock_response = MagicMock()
    mock_response.status_code = 402
    http_err = _requests.exceptions.HTTPError(response=mock_response)

    mock_client = type("C", (), {
        "transcript": staticmethod(lambda **kw: (_ for _ in ()).throw(http_err))
    })()

    event = threading.Event()
    result = fetch_transcript("VID123", mock_client, transcript_rate_limit_event=event)
    assert result is None
    assert event.is_set()


def test_fetch_transcript_does_not_set_event_on_other_errors(monkeypatch):
    """fetch_transcript does NOT set the event for non-quota errors (e.g. video not found)."""
    import threading
    from supadata import SupadataError

    mock_client = type("C", (), {
        "transcript": staticmethod(lambda **kw: (_ for _ in ()).throw(
            SupadataError(error="video-not-found", message="Not found", details="")
        ))
    })()

    event = threading.Event()
    result = fetch_transcript("VID123", mock_client, transcript_rate_limit_event=event)
    assert result is None
    assert not event.is_set()


def test_process_feed_stops_on_transcript_quota_exhausted(tmp_path, monkeypatch):
    """process_feed must stop processing further videos once transcript quota is exhausted."""
    import threading
    import TubeNews
    monkeypatch.setattr(TubeNews, "STORAGE_ROOT", tmp_path)

    yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
    monkeypatch.setattr(TubeNews, "discover_videos", lambda *a, **kw: [
        {"id": "VID_A", "title": "Meeting A", "date": yesterday, "is_live": False},
        {"id": "VID_B", "title": "Meeting B", "date": yesterday, "is_live": False},
    ])

    calls = []

    def fake_fetch(video_id, client, feed_name="", video_title="",
                   transcript_rate_limit_event=None):
        calls.append(video_id)
        if transcript_rate_limit_event is not None:
            transcript_rate_limit_event.set()
        return None

    monkeypatch.setattr(TubeNews, "fetch_transcript", fake_fetch)

    from unittest.mock import MagicMock
    feed = {"channel_id": "UCtest1234567890", "channel_name": "Test Channel", "focus": "test"}
    event = threading.Event()
    process_feed(feed, MagicMock(), {"gemini_api_key": "k", "gemini_model": "m"},
                 transcript_rate_limit_event=event)

    # Only one video should have been attempted before the event stopped the loop.
    assert len(calls) == 1
    assert event.is_set()
