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
    build_user_feed_xml,
    write_story_files,
    _relative_date_to_iso,
    _parse_channel_page_metadata,
    _resolve_early_config,
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
    assert not (user_archive / "users" / "Jane_Doe" / "rss.xml").exists()

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
    written = (user_archive / "users" / "Compare" / "rss.xml").read_bytes()

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
    blog_html = (parity_archive / "users" / "Parity" / "index.html").read_text()

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
    blog_html = (parity_archive / "users" / "Selective" / "index.html").read_text()

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
    blog_html = (parity_archive / "users" / "Empty" / "index.html").read_text()

    assert "Channel A" not in feed_xml
    assert "Channel B" not in feed_xml
    assert "Channel A" not in blog_html
    assert "Channel B" not in blog_html


def test_blog_date_filter_does_not_affect_feed(tmp_path, monkeypatch):
    """An old story (beyond blog_days) is absent from the blog but still present
    in the RSS feed, which has no date filter."""
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
    rebuild_user_blog(user, blog_days=90)
    blog_html = (tmp_path / "users" / "Time" / "index.html").read_text()

    assert "Ancient Story" in feed_xml,      "Feed has no date filter — old stories must still appear"
    assert "Ancient Story" not in blog_html, "Blog date filter must exclude old stories"


# ---------------------------------------------------------------------------
# _resolve_early_config — archive_dir and request_timeout
# ---------------------------------------------------------------------------

def test_resolve_archive_dir_absolute(tmp_path):
    """An absolute archive_dir is used as-is for STORAGE_ROOT."""
    custom = tmp_path / "my_archive"
    cfg = tmp_path / "TubeNews.json"
    cfg.write_text(json.dumps({"archive_dir": str(custom)}))
    storage_root, _ = _resolve_early_config(cfg, tmp_path)
    assert storage_root == custom


def test_resolve_archive_dir_relative(tmp_path):
    """A relative archive_dir is resolved against base_dir."""
    cfg = tmp_path / "TubeNews.json"
    cfg.write_text(json.dumps({"archive_dir": "subdir/archive"}))
    storage_root, _ = _resolve_early_config(cfg, tmp_path)
    assert storage_root == (tmp_path / "subdir" / "archive").resolve()


def test_resolve_archive_dir_absent_defaults_to_base_archive(tmp_path):
    """When archive_dir is omitted, STORAGE_ROOT defaults to base_dir/archive."""
    cfg = tmp_path / "TubeNews.json"
    cfg.write_text(json.dumps({"gemini_api_key": "x"}))
    storage_root, _ = _resolve_early_config(cfg, tmp_path)
    assert storage_root == tmp_path / "archive"


def test_resolve_archive_dir_empty_string_defaults_to_base_archive(tmp_path):
    """An explicit empty string for archive_dir is treated the same as absent."""
    cfg = tmp_path / "TubeNews.json"
    cfg.write_text(json.dumps({"archive_dir": ""}))
    storage_root, _ = _resolve_early_config(cfg, tmp_path)
    assert storage_root == tmp_path / "archive"


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
    assert storage_root == tmp_path / "archive"
    assert timeout == 15


def test_resolve_falls_back_on_invalid_json(tmp_path):
    """Corrupt JSON must not crash — defaults are returned instead."""
    cfg = tmp_path / "TubeNews.json"
    cfg.write_text("{ NOT VALID JSON }")
    storage_root, timeout = _resolve_early_config(cfg, tmp_path)
    assert storage_root == tmp_path / "archive"
    assert timeout == 15


def test_resolve_request_timeout_is_int_not_string(tmp_path):
    """request_timeout must be returned as int even if stored as a JSON number."""
    cfg = tmp_path / "TubeNews.json"
    cfg.write_text(json.dumps({"request_timeout": 45}))
    _, timeout = _resolve_early_config(cfg, tmp_path)
    assert isinstance(timeout, int)
