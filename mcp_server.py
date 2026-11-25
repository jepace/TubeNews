#!/usr/bin/env python3
"""MCP server exposing the TubeNews archive to an AI client.

Runs over stdio and reads the archive directly, so it must run on the
machine that holds ``content/`` — on the server, reached over SSH:

    {"mcpServers": {"tubenews": {"command": "ssh",
      "args": ["tubenews-host", "python3 /var/www/tubenews/mcp_server.py"]}}}

Read-only by construction: nothing here writes, and no tool takes a user id
or touches ``state/``. Per-user attribution and read/star state are serving
concerns; this searches the archive as a whole.

All search logic lives in ``tubenews_search``, which has no MCP dependency,
so the same functions can back a web search page or an HTTP transport.
"""
from __future__ import annotations

import sys
from pathlib import Path

from mcp.server.mcpserver import MCPServer

sys.path.insert(0, str(Path(__file__).resolve().parent))

from tubenews_search import (  # noqa: E402  (after sys.path setup)
    get_story as _get_story,
    list_channels as _list_channels,
    search_stories as _search_stories,
    search_transcripts as _search_transcripts,
)
from tubenews_utils import resolve_roots  # noqa: E402

_BASE_DIR = Path(__file__).resolve().parent
STORAGE_ROOT, _STATE_ROOT = resolve_roots(_BASE_DIR / "TubeNews.json", _BASE_DIR)


def _base_url() -> str:
    """Site root for building article links, read fresh so a config edit lands."""
    import json
    try:
        return json.loads(
            (_BASE_DIR / "TubeNews.json").read_text(encoding="utf-8")
        ).get("base_url", "").rstrip("/")
    except (OSError, ValueError):
        return ""


server = MCPServer(
    name="tubenews",
    instructions=(
        "Search a local-government news archive built from meeting videos.\n\n"
        "Two corpora, and the difference decides which tool to use:\n"
        "- search_stories covers AI-written news stories. These exist only "
        "where some subscriber's topic of interest matched, so the archive "
        "of stories is a filtered view.\n"
        "- search_transcripts covers what was actually said in every meeting, "
        "including everything no story was ever written about. Use it when "
        "the stories turn up nothing, or when asked whether a subject came "
        "up at all. Results carry a timestamp into the video.\n\n"
        "Queries are AND: every bare word and every \"quoted phrase\" must "
        "appear. Prefer distinctive words; these are meeting transcripts, so "
        "common civic vocabulary matches nearly everything."
    ),
)


@server.tool()
def search_stories(query: str, channel: str = "", since: str = "",
                   until: str = "", limit: int = 20) -> dict:
    """Search AI-written news stories by keyword or "quoted phrase".

    Args:
        query: Words that must all appear; wrap exact wording in double quotes.
        channel: Optional substring of a channel name, e.g. "Monterey".
        since: Optional earliest date, YYYY-MM-DD.
        until: Optional latest date, YYYY-MM-DD.
        limit: Maximum results (default 20).
    """
    hits = _search_stories(STORAGE_ROOT, query, channel=channel, since=since,
                           until=until, limit=max(1, min(limit, 100)))
    root = _base_url()
    return {
        "query": query,
        "count": len(hits),
        "results": [h.as_dict(root) for h in hits],
        "note": (
            "No stories matched. Stories are only written where a subscriber's "
            "topic matched, so try search_transcripts, which covers everything "
            "that was said." if not hits else ""
        ),
    }


@server.tool()
def search_transcripts(query: str, channel: str = "", since: str = "",
                       until: str = "", limit: int = 20) -> dict:
    """Search full meeting transcripts, including meetings no story covered.

    Returns one hit per meeting, with a timestamp linking to that moment in
    the video. ``meeting_has_stories: false`` marks a meeting the news feed
    never wrote up at all.

    Args:
        query: Words that must all appear; wrap exact wording in double quotes.
        channel: Optional substring of a channel name, e.g. "Monterey".
        since: Optional earliest date, YYYY-MM-DD.
        until: Optional latest date, YYYY-MM-DD.
        limit: Maximum results (default 20).
    """
    hits = _search_transcripts(STORAGE_ROOT, query, channel=channel, since=since,
                               until=until, limit=max(1, min(limit, 100)))
    root = _base_url()
    return {"query": query, "count": len(hits),
            "results": [h.as_dict(root) for h in hits]}


@server.tool()
def get_story(video_id: str, start_seconds: int = 0) -> dict:
    """Fetch one story in full, as Markdown.

    Args:
        video_id: YouTube video id, from a search result.
        start_seconds: The story's offset into the video, from a search result.
    """
    story = _get_story(STORAGE_ROOT, video_id, start_seconds)
    if story is None:
        return {"error": f"No story found for video {video_id} at {start_seconds}s"}
    return story


@server.tool()
def list_channels() -> dict:
    """List the channels in the archive, with story and transcript counts.

    Call this first when a question names a place or body, to find the
    channel string that search_stories and search_transcripts will match.
    """
    rows = _list_channels(STORAGE_ROOT)
    return {
        "count": len(rows),
        "channels": rows,
        "totals": {
            "meetings": sum(r["meetings"] for r in rows),
            "stories": sum(r["stories"] for r in rows),
            "transcripts": sum(r["transcripts"] for r in rows),
        },
    }


if __name__ == "__main__":
    if not STORAGE_ROOT.is_dir():
        print(f"TubeNews MCP: no archive at {STORAGE_ROOT} — "
              "run this on the machine holding content/", file=sys.stderr)
    server.run()
