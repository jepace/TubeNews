"""Tests for the archive search layer behind the MCP server.

Run with:  pytest tests/test_search.py -v
"""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tubenews_search import (  # noqa: E402
    get_story, list_channels, parse_query, search_stories, search_transcripts,
)


@pytest.fixture
def archive(tmp_path):
    """A small archive: one meeting with a story, one with only a transcript."""
    root = tmp_path / "content"

    monterey = root / "county_of_monterey" / "abc123"
    monterey.mkdir(parents=True)
    (root / "county_of_monterey" / "channel.json").write_text(
        json.dumps({"channel_name": "County of Monterey"}))
    (monterey / "metadata.json").write_text(json.dumps({
        "video_id": "abc123", "video_title": "Planning Commission 10/14",
        "video_date": "2026-10-14", "processed_at": "2026-10-14T17:02:00Z",
    }))
    (monterey / "01_housing.md").write_text(
        "# Planning Commission Advances Housing Element Update\n"
        "*SALINAS, Calif. — October 14, 2026*\n"
        "**Source:** https://youtu.be/abc123?t=1450\n\n"
        "The commission voted to recommend approval of a housing element "
        "update, citing the builder's remedy.\n\n"
        "---\n**Segment Start:** 1450s\n")
    (monterey / "transcript.txt").write_text(
        "0s --> Good morning, call to order.\n"
        "1200s --> Next item, the Howard Road culvert replacement.\n"
        "1250s --> Staff recommends deferring the culvert work.\n")

    brisbane = root / "city_of_brisbane" / "xyz789"
    brisbane.mkdir(parents=True)
    (root / "city_of_brisbane" / "channel.json").write_text(
        json.dumps({"channel_name": "City of Brisbane"}))
    (brisbane / "metadata.json").write_text(json.dumps({
        "video_id": "xyz789", "video_title": "City Council 10/20",
        "video_date": "2026-10-20", "processed_at": "2026-10-20T01:00:00Z",
    }))
    (brisbane / "transcript.txt").write_text(
        "300s --> We received a grant for the Howard Road sidewalk project.\n")
    return root


# -- query parsing --

def test_parse_query_separates_phrases_from_terms():
    q = parse_query('water rates "general plan"')
    assert q.phrases == ["general plan"]
    assert q.terms == ["water", "rates"]


def test_query_terms_are_all_required(archive):
    """AND, not OR. An archive search that matched any word would bury the hit."""
    assert search_transcripts(archive, "culvert") != []
    assert search_transcripts(archive, "culvert grant") == [], (
        "terms from two different meetings must not produce a match"
    )


def test_empty_query_returns_nothing(archive):
    """A blank query must not dump the whole archive."""
    assert search_stories(archive, "   ") == []
    assert search_transcripts(archive, "") == []


# -- stories --

def test_search_stories_matches_title_and_body(archive):
    assert [h.title for h in search_stories(archive, '"housing element"')] == [
        "Planning Commission Advances Housing Element Update"]
    assert search_stories(archive, "builder's remedy") != []


def test_search_stories_reports_the_segment_start(archive):
    """The story's offset is parsed so results link to the right moment."""
    hit = search_stories(archive, '"housing element"')[0]
    assert hit.start_seconds == 1450
    assert hit.as_dict("https://x.test")["video_url"] == "https://youtu.be/abc123?t=1450"
    assert hit.as_dict("https://x.test")["article_url"] == "https://x.test/article/abc123/1450"


def test_title_matches_outrank_body_matches(archive, tmp_path):
    """A story about the search term beats one that merely mentions it."""
    other = archive / "county_of_monterey" / "def456"
    other.mkdir()
    (other / "metadata.json").write_text(json.dumps(
        {"video_id": "def456", "processed_at": "2026-10-15T00:00:00Z"}))
    (other / "01_budget.md").write_text(
        "# Budget Hearing Opens\n*SALINAS*\n\nThe board also noted housing "
        "element progress.\n\n---\n**Segment Start:** 10s\n")
    titles = [h.title for h in search_stories(archive, '"housing element"')]
    assert titles[0].startswith("Planning Commission"), titles


def test_a_topic_nobody_subscribed_to_has_no_story(archive):
    """The premise for transcript search: stories are a filtered view."""
    assert search_stories(archive, "culvert") == []
    assert search_transcripts(archive, "culvert") != []


# -- transcripts --

def test_transcript_hit_timestamps_the_matching_segment(archive):
    """Regression: the hit must point at the words, not at the window's start.

    Matching runs over a window of segments so a phrase split by the
    transcriber is still found. Reporting the window's first segment
    timestamped this hit at 0:00 instead of 20:00 — minutes before the
    moment, which defeats the point of a timestamped result.
    """
    hit = search_transcripts(archive, "culvert")[0]
    assert hit.offset_seconds == 1200
    assert hit.as_dict()["at"] == "20:00"
    assert hit.as_dict()["video_url"].endswith("?t=1200")


def test_transcript_search_finds_a_phrase_split_across_segments(archive):
    """The window is why this works at all."""
    hits = search_transcripts(archive, '"call to order" "howard road"')
    assert len(hits) == 1


def test_transcript_hit_flags_a_meeting_with_no_stories(archive):
    """meeting_has_stories marks what the news feed never wrote up."""
    by_channel = {h.channel_name: h.as_dict()
                  for h in search_transcripts(archive, '"howard road"')}
    assert by_channel["City of Brisbane"]["meeting_has_stories"] is False
    assert by_channel["County of Monterey"]["meeting_has_stories"] is True


def test_transcript_results_link_to_the_transcript_page(archive):
    hit = [h for h in search_transcripts(archive, "sidewalk")][0]
    assert hit.as_dict("https://x.test")["transcript_url"] == (
        "https://x.test/transcript/city_of_brisbane/xyz789#t300")


def test_one_hit_per_meeting(archive):
    """Two matches in one meeting return one result, not a flood."""
    assert len(search_transcripts(archive, "culvert")) == 1


# -- filters --

def test_channel_filter_matches_name_or_slug(archive):
    assert [h.channel_name for h in
            search_transcripts(archive, '"howard road"', channel="brisbane")] == [
        "City of Brisbane"]
    assert [h.channel_name for h in
            search_transcripts(archive, '"howard road"', channel="County of Mont")] == [
        "County of Monterey"]


def test_date_filters_bound_the_window(archive):
    assert [h.channel_name for h in
            search_transcripts(archive, '"howard road"', since="2026-10-18")] == [
        "City of Brisbane"]
    assert [h.channel_name for h in
            search_transcripts(archive, '"howard road"', until="2026-10-18")] == [
        "County of Monterey"]


def test_limit_is_respected(archive):
    assert len(search_transcripts(archive, '"howard road"', limit=1)) == 1


# -- get_story / list_channels --

def test_get_story_returns_the_full_markdown(archive):
    story = get_story(archive, "abc123", 1450)
    assert story["title"] == "Planning Commission Advances Housing Element Update"
    assert "builder's remedy" in story["markdown"]
    assert story["channel"] == "County of Monterey"


def test_get_story_returns_none_for_a_wrong_offset(archive):
    assert get_story(archive, "abc123", 9999) is None


def test_list_channels_counts_stories_and_transcripts(archive):
    rows = {r["channel"]: r for r in list_channels(archive)}
    assert rows["County of Monterey"]["stories"] == 1
    assert rows["City of Brisbane"]["stories"] == 0
    assert rows["City of Brisbane"]["transcripts"] == 1
    assert rows["City of Brisbane"]["latest"] == "2026-10-20"


def test_a_missing_archive_is_not_an_error(tmp_path):
    """Running where content/ does not exist returns nothing, never raises."""
    nowhere = tmp_path / "absent"
    assert list_channels(nowhere) == []
    assert search_stories(nowhere, "anything") == []
    assert search_transcripts(nowhere, "anything") == []


def test_meetings_without_metadata_are_skipped(archive):
    """A half-written directory must not break a search."""
    (archive / "county_of_monterey" / "partial").mkdir()
    assert search_transcripts(archive, "culvert") != []
