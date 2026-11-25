"""Read-only search over the TubeNews archive.

Deliberately free of any MCP or Flask dependency: ``mcp_server.py`` wraps
these functions for an AI client, and the web app can use the same ones for
a search page without either importing the other.

Two corpora, and the difference matters:

* **Stories** are what Gemini wrote, and it only writes what some user's
  focus asked for. A topic nobody subscribed to was read and discarded.
* **Transcripts** are everything that was said. Searching them finds what
  the stories never covered, which is usually the point of going back
  through an archive.

Both are plain files under ``content/<channel_slug>/<video_id>/``, so search
is a scan. No index is built: an index is another thing to keep in step with
the filesystem, and the archive is small enough that scanning is honest.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator

# A transcript line is "<offset_seconds>s --> <text>" (see fetch_transcript).
_TRANSCRIPT_LINE = re.compile(r"^(\d+)s --> (.*)$")
_SNIPPET_RADIUS = 160


@dataclass
class StoryHit:  # pylint: disable=too-many-instance-attributes
    """One matching story. A flat record of what a caller needs to show it."""
    title: str
    channel_name: str
    channel_slug: str
    video_id: str
    start_seconds: int
    dateline: str
    processed_at: str
    snippet: str
    score: int = 0

    def as_dict(self, base_url: str = "") -> dict:
        root = base_url.rstrip("/")
        return {
            "title": self.title,
            "channel": self.channel_name,
            "dateline": self.dateline,
            "processed_at": self.processed_at,
            "snippet": self.snippet,
            "article_url": f"{root}/article/{self.video_id}/{self.start_seconds}",
            "video_url": f"https://youtu.be/{self.video_id}?t={self.start_seconds}",
            "video_id": self.video_id,
            "start_seconds": self.start_seconds,
        }


@dataclass
class TranscriptHit:  # pylint: disable=too-many-instance-attributes
    """One matching moment in a transcript. Flat for the same reason as StoryHit."""
    channel_name: str
    channel_slug: str
    video_id: str
    video_title: str
    video_date: str
    offset_seconds: int
    snippet: str
    has_story: bool = False
    score: int = 0

    def as_dict(self, base_url: str = "") -> dict:
        root = base_url.rstrip("/")
        return {
            "channel": self.channel_name,
            "video_title": self.video_title,
            "video_date": self.video_date,
            "at": _hhmmss(self.offset_seconds),
            "offset_seconds": self.offset_seconds,
            "snippet": self.snippet,
            "video_url": f"https://youtu.be/{self.video_id}?t={self.offset_seconds}",
            "transcript_url":
                f"{root}/transcript/{self.channel_slug}/{self.video_id}"
                f"#t{self.offset_seconds}",
            # Whether this meeting produced any story at all. A hit in a
            # meeting with no story is exactly the thing the feed missed.
            "meeting_has_stories": self.has_story,
        }


@dataclass
class Query:
    """A parsed search query: quoted phrases plus bare terms, all required."""
    phrases: list[str] = field(default_factory=list)
    terms: list[str] = field(default_factory=list)

    def __bool__(self) -> bool:
        return bool(self.phrases or self.terms)


def _hhmmss(seconds: int) -> str:
    hours, rem = divmod(max(0, seconds), 3600)
    minutes, secs = divmod(rem, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes}:{secs:02d}"


def parse_query(raw: str) -> Query:
    """Split *raw* into quoted phrases and bare terms.

    ``water rates "general plan"`` requires both the two words and the exact
    phrase. Everything is required (AND): an archive search that returned
    anything matching any word would bury the one meeting you wanted.
    """
    phrases = [p.strip().lower() for p in re.findall(r'"([^"]+)"', raw) if p.strip()]
    remainder = re.sub(r'"[^"]*"', " ", raw)
    terms = [t.lower() for t in re.findall(r"[\w'-]+", remainder)]
    return Query(phrases=phrases, terms=terms)


def _matches(query: Query, haystack_lower: str) -> bool:
    return (all(p in haystack_lower for p in query.phrases)
            and all(t in haystack_lower for t in query.terms))


def _snippet(text: str, query: Query, radius: int = _SNIPPET_RADIUS) -> str:
    """Return text around the first match, so a hit can be judged without opening it."""
    lowered = text.lower()
    needle = (query.phrases + query.terms or [""])[0]
    pos = lowered.find(needle) if needle else -1
    if pos < 0:
        return " ".join(text.split())[: radius * 2].strip()
    start = max(0, pos - radius)
    end = min(len(text), pos + len(needle) + radius)
    body = " ".join(text[start:end].split())
    return ("…" if start else "") + body + ("…" if end < len(text) else "")


def _score(query: Query, title: str, body: str) -> int:
    """Rank by where the match landed; a title hit is worth far more than a body one."""
    title_l, body_l = title.lower(), body.lower()
    score = 0
    for needle in query.phrases:
        score += 40 * title_l.count(needle) + 6 * body_l.count(needle)
    for needle in query.terms:
        score += 20 * title_l.count(needle) + body_l.count(needle)
    return score


def _first_matching_offset(window: list[tuple[int, str]], query: Query,
                           default: int) -> int:
    """Offset of the first segment in *window* containing any search term."""
    needles = query.phrases + query.terms
    for offset, text in window:
        lowered = text.lower()
        if any(needle in lowered for needle in needles):
            return offset
    return default


def iter_meetings(storage_root: Path) -> Iterator[tuple[Path, Path, dict, str]]:
    """Yield ``(channel_dir, meeting_dir, metadata, channel_name)`` for the archive."""
    if not storage_root.is_dir():
        return
    for channel_dir in sorted(storage_root.iterdir()):
        if not channel_dir.is_dir() or channel_dir.name.startswith(("_", ".")):
            continue
        if channel_dir.name == "users":
            continue
        channel_name = channel_dir.name.replace("_", " ")
        channel_json = channel_dir / "channel.json"
        if channel_json.exists():
            try:
                channel_name = json.loads(
                    channel_json.read_text(encoding="utf-8")
                ).get("channel_name", channel_name)
            except (OSError, ValueError):
                pass
        for meeting_dir in sorted(channel_dir.iterdir()):
            if not meeting_dir.is_dir():
                continue
            meta_path = meeting_dir / "metadata.json"
            if not meta_path.exists():
                continue
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            yield channel_dir, meeting_dir, meta, channel_name


def _in_window(value: str, since: str, until: str) -> bool:
    """Compare ``YYYY-MM-DD`` prefixes; an empty bound is open."""
    day = (value or "")[:10]
    if since and day and day < since[:10]:
        return False
    if until and day and day > until[:10]:
        return False
    return True


def search_stories(storage_root: Path, query_text: str, channel: str = "",
                   since: str = "", until: str = "", limit: int = 20) -> list[StoryHit]:
    """Search story titles and bodies. Highest-scoring first."""
    query = parse_query(query_text)
    if not query:
        return []
    channel_l = channel.lower().strip()
    hits: list[StoryHit] = []

    for channel_dir, meeting_dir, meta, channel_name in iter_meetings(storage_root):
        if channel_l and channel_l not in channel_name.lower() \
                and channel_l not in channel_dir.name.lower():
            continue
        if not _in_window(str(meta.get("processed_at", "")), since, until):
            continue
        for story_file in sorted(meeting_dir.glob("[0-9]*.md")):
            try:
                text = story_file.read_text(encoding="utf-8")
            except OSError:
                continue
            if not _matches(query, text.lower()):
                continue
            lines = text.splitlines()
            title = lines[0].lstrip("# ").strip() if lines else story_file.stem
            dateline = lines[1].replace("*", "").strip() if len(lines) > 1 else ""
            start = 0
            start_match = re.search(r"\*\*Segment Start:\*\*\s*(\d+)s", text)
            if start_match:
                start = int(start_match.group(1))
            hits.append(StoryHit(
                title=title, channel_name=channel_name,
                channel_slug=channel_dir.name,
                video_id=str(meta.get("video_id", meeting_dir.name)),
                start_seconds=start, dateline=dateline,
                processed_at=str(meta.get("processed_at", "")),
                snippet=_snippet(text, query),
                score=_score(query, title, text),
            ))

    # Best match first; newest first among equal scores.
    hits.sort(key=lambda h: (h.score, h.processed_at), reverse=True)
    return hits[:limit]


def search_transcripts(storage_root: Path, query_text: str, channel: str = "",
                       since: str = "", until: str = "", limit: int = 20,
                       context_lines: int = 2) -> list[TranscriptHit]:
    """Search raw transcripts and return the moments that matched.

    This reaches what the stories do not: Gemini only wrote up what some
    user's focus asked for, so a topic nobody subscribed to was read and
    discarded. Each hit carries the timestamp, so it links to the moment in
    the video and to the same point in the transcript page.
    """
    query = parse_query(query_text)
    if not query:
        return []
    channel_l = channel.lower().strip()
    hits: list[TranscriptHit] = []

    for channel_dir, meeting_dir, meta, channel_name in iter_meetings(storage_root):
        if channel_l and channel_l not in channel_name.lower() \
                and channel_l not in channel_dir.name.lower():
            continue
        if not _in_window(str(meta.get("video_date") or meta.get("processed_at", "")),
                          since, until):
            continue
        transcript = meeting_dir / "transcript.txt"
        if not transcript.exists():
            continue
        try:
            raw = transcript.read_text(encoding="utf-8")
        except OSError:
            continue
        if not _matches(query, raw.lower()):
            continue

        segments: list[tuple[int, str]] = []
        for line in raw.splitlines():
            parsed = _TRANSCRIPT_LINE.match(line)
            if parsed:
                segments.append((int(parsed.group(1)), parsed.group(2)))
            elif segments:
                # A continuation line belongs to the segment above it.
                segments[-1] = (segments[-1][0], segments[-1][1] + " " + line)
            else:
                segments.append((0, line))

        has_story = any(meeting_dir.glob("[0-9]*.md"))
        for index, (offset, text) in enumerate(segments):
            # Match against a window, so a phrase split across segments by
            # the transcriber is still found.
            window = " ".join(
                seg_text for _, seg_text in
                segments[index: index + 1 + context_lines]
            )
            if not _matches(query, window.lower()):
                continue
            # Report the segment the words are actually in, not the start of
            # the window that matched. The window exists so a phrase split
            # across segments is still found, but a hit that timestamped the
            # window's first segment could point minutes before the moment,
            # and jumping to the moment is the whole value of the hit.
            offset = _first_matching_offset(
                segments[index: index + 1 + context_lines], query, offset)
            hits.append(TranscriptHit(
                channel_name=channel_name, channel_slug=channel_dir.name,
                video_id=str(meta.get("video_id", meeting_dir.name)),
                video_title=str(meta.get("video_title", "")),
                video_date=str(meta.get("video_date", ""))[:10],
                offset_seconds=offset,
                snippet=_snippet(window, query),
                has_story=has_story,
                score=_score(query, str(meta.get("video_title", "")), window),
            ))
            break  # One hit per meeting; the timestamp points at the rest.

    hits.sort(key=lambda h: (h.score, h.video_date), reverse=True)
    return hits[:limit]


def get_story(storage_root: Path, video_id: str, start_seconds: int) -> dict | None:
    """Return one story in full, located the same way the web /article route locates it."""
    for channel_dir, meeting_dir, meta, channel_name in iter_meetings(storage_root):
        if str(meta.get("video_id", "")) != video_id:
            continue
        for story_file in sorted(meeting_dir.glob("[0-9]*.md")):
            try:
                text = story_file.read_text(encoding="utf-8")
            except OSError:
                continue
            found = re.search(r"\*\*Segment Start:\*\*\s*(\d+)s", text)
            if (int(found.group(1)) if found else 0) != start_seconds:
                continue
            lines = text.splitlines()
            return {
                "title": lines[0].lstrip("# ").strip() if lines else "",
                "dateline": lines[1].replace("*", "").strip() if len(lines) > 1 else "",
                "channel": channel_name,
                "video_title": str(meta.get("video_title", "")),
                "video_date": str(meta.get("video_date", ""))[:10],
                "markdown": text,
                "video_url": f"https://youtu.be/{video_id}?t={start_seconds}",
            }
    return None


def list_channels(storage_root: Path) -> list[dict]:
    """Summarise the archive: one row per channel, with counts and date range."""
    summary: dict[str, dict] = {}
    for channel_dir, meeting_dir, meta, channel_name in iter_meetings(storage_root):
        row = summary.setdefault(channel_dir.name, {
            "channel": channel_name, "slug": channel_dir.name,
            "meetings": 0, "stories": 0, "transcripts": 0,
            "earliest": "", "latest": "",
        })
        row["meetings"] += 1
        row["stories"] += len(list(meeting_dir.glob("[0-9]*.md")))
        if (meeting_dir / "transcript.txt").exists():
            row["transcripts"] += 1
        day = str(meta.get("video_date") or meta.get("processed_at", ""))[:10]
        if day:
            row["earliest"] = min(row["earliest"] or day, day)
            row["latest"] = max(row["latest"], day)
    return sorted(summary.values(), key=lambda r: r["channel"].lower())
