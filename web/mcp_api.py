"""MCP endpoint for the web app: JSON-RPC over ``POST /mcp``, bearer-authenticated.

Why this lives in Flask rather than beside the SDK's own ASGI app: the app
already holds the users, the rate limiter and the admin UI, which is where
an abusive key is noticed and shut off. A second service would need its own
copy of all three.

Every request carries a per-user key (``Authorization: Bearer tnk_…``), and
every tool call is **scoped to that user's subscriptions and story
attribution**, exactly as their web feed is. A key therefore grants no more
than the account it belongs to, and a leaked one leaks nothing extra.

The protocol surface is deliberately small — ``initialize``, ``tools/list``,
``tools/call``, ``ping`` — and answers with plain JSON rather than an event
stream. The spec permits a JSON response to a POST, and a streaming
transport would buy nothing for request/response search.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Callable

from flask import Blueprint, Response, request

import tubenews_apikeys as apikeys
from tubenews_search import (
    get_story as _get_story,
    list_channels as _list_channels,
    search_stories as _search_stories,
    search_transcripts as _search_transcripts,
)

logger = logging.getLogger(__name__)

mcp_bp = Blueprint("mcp", __name__)

PROTOCOL_VERSION = "2025-06-18"
_MAX_LIMIT = 50

# JSON-RPC error codes (the standard ones plus the HTTP-ish auth cases).
_PARSE_ERROR = -32700
_INVALID_REQUEST = -32600
_METHOD_NOT_FOUND = -32601
_INVALID_PARAMS = -32602
_INTERNAL_ERROR = -32603

_SERVER_INSTRUCTIONS = (
    "Search a local-government news archive built from meeting videos. "
    "Results are limited to the channels this account subscribes to.\n\n"
    "Two corpora, and the difference decides which tool to use:\n"
    "- search_stories covers AI-written news stories, which exist only where "
    "this account's topics of interest matched. It is a filtered view.\n"
    "- search_transcripts covers what was actually said in every meeting, "
    "including everything no story was written about. Use it when the "
    "stories turn up nothing, or when asked whether a subject came up at "
    "all. Results carry a timestamp into the video.\n\n"
    "Queries are AND: every bare word and every \"quoted phrase\" must "
    "appear. Prefer distinctive words; these are meeting transcripts, so "
    "common civic vocabulary matches nearly everything."
)

_SEARCH_PROPERTIES = {
    "query": {"type": "string",
              "description": 'Words that must all appear; wrap exact wording in "double quotes".'},
    "channel": {"type": "string",
                "description": 'Optional substring of a channel name, e.g. "Monterey".'},
    "since": {"type": "string", "description": "Optional earliest date, YYYY-MM-DD."},
    "until": {"type": "string", "description": "Optional latest date, YYYY-MM-DD."},
    "limit": {"type": "integer", "description": f"Maximum results (1-{_MAX_LIMIT}, default 20)."},
}

TOOLS: list[dict] = [
    {
        "name": "search_stories",
        "description": (
            "Search AI-written news stories from meetings this account "
            "subscribes to. Stories exist only where the account's topics "
            "matched, so a subject nobody follows will not be here — use "
            "search_transcripts for that."
        ),
        "inputSchema": {"type": "object", "properties": _SEARCH_PROPERTIES,
                        "required": ["query"]},
    },
    {
        "name": "search_transcripts",
        "description": (
            "Search full meeting transcripts, including meetings no story "
            "covered. Returns one hit per meeting with a timestamp into the "
            "video; meeting_has_stories=false marks a meeting the news feed "
            "never wrote up."
        ),
        "inputSchema": {"type": "object", "properties": _SEARCH_PROPERTIES,
                        "required": ["query"]},
    },
    {
        "name": "get_story",
        "description": "Fetch one story in full, as Markdown, using ids from a search result.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "video_id": {"type": "string", "description": "YouTube video id from a search result."},
                "start_seconds": {"type": "integer",
                                  "description": "The story's offset into the video, from a search result."},
            },
            "required": ["video_id"],
        },
    },
    {
        "name": "list_channels",
        "description": (
            "List the channels this account subscribes to, with story and "
            "transcript counts. Call this first when a question names a "
            "place or body, to find the channel string to filter on."
        ),
        "inputSchema": {"type": "object", "properties": {}},
    },
]


def bearer_token() -> str:
    """The bearer token on this request, or ''. Used for auth and for rate limiting."""
    header = request.headers.get("Authorization", "")
    if header.lower().startswith("bearer "):
        return header[7:].strip()
    return ""


def rate_limit_key() -> str:
    """Rate-limit per key, falling back to the client address.

    Limiting by address would punish everyone behind one NAT for one bad
    agent, and would not restrain an agent that moves between addresses.
    The key is the thing being abused, so it is the thing to limit. The
    token is hashed so the limiter never holds a usable secret in memory.
    """
    token = bearer_token()
    if token:
        return "mcpkey:" + apikeys.hash_key(token)[:32]
    return "mcpaddr:" + (request.remote_addr or "unknown")


def _error(req_id: Any, code: int, message: str, http_status: int = 200) -> Response:
    body = {"jsonrpc": "2.0", "id": req_id,
            "error": {"code": code, "message": message}}
    return Response(json.dumps(body), status=http_status,
                    mimetype="application/json")


def _result(req_id: Any, result: dict) -> Response:
    return Response(json.dumps({"jsonrpc": "2.0", "id": req_id, "result": result}),
                    mimetype="application/json")


def _tool_result(payload: dict) -> dict:
    """Wrap a tool's return value in MCP's content envelope.

    The JSON goes in a text block *and* in structuredContent: clients that
    understand structured output get parsed data, and the rest get something
    a model can still read.
    """
    text = json.dumps(payload, indent=2, ensure_ascii=False)
    return {"content": [{"type": "text", "text": text}],
            "structuredContent": payload, "isError": False}


def _tool_error(message: str) -> dict:
    return {"content": [{"type": "text", "text": message}], "isError": True}


def _clamp_limit(raw: Any) -> int:
    try:
        return max(1, min(int(raw), _MAX_LIMIT))
    except (TypeError, ValueError):
        return 20


def run_tool(name: str, args: dict, *, storage_root, allowed_channel_ids: set[str],
             user_id: str, base_url: str) -> dict:
    """Dispatch one tool call. Scoping is applied here, not by the caller."""
    # Check the name before anything else. Validating arguments first made an
    # unknown tool report "query is required", which tells a client to fix
    # the wrong thing.
    if name not in {t["name"] for t in TOOLS}:
        raise KeyError(name)

    if name == "list_channels":
        rows = _list_channels(storage_root, allowed_channel_ids)
        return {"count": len(rows), "channels": rows,
                "totals": {
                    "meetings": sum(r["meetings"] for r in rows),
                    "stories": sum(r["stories"] for r in rows),
                    "transcripts": sum(r["transcripts"] for r in rows),
                }}

    if name == "get_story":
        video_id = str(args.get("video_id", "")).strip()
        if not video_id:
            raise ValueError("video_id is required")
        story = _get_story(storage_root, video_id,
                           int(args.get("start_seconds", 0) or 0),
                           allowed_channel_ids=allowed_channel_ids,
                           user_id=user_id)
        if story is None:
            return {"error": "No such story in the channels this account subscribes to."}
        return story

    query = str(args.get("query", "")).strip()
    if not query:
        raise ValueError("query is required")
    channel = str(args.get("channel", "") or "")
    since = str(args.get("since", "") or "")
    until = str(args.get("until", "") or "")
    limit = _clamp_limit(args.get("limit", 20))

    if name == "search_stories":
        story_hits = _search_stories(
            storage_root, query, channel=channel, since=since, until=until,
            limit=limit, allowed_channel_ids=allowed_channel_ids, user_id=user_id)
        return {
            "query": query, "count": len(story_hits),
            "results": [h.as_dict(base_url) for h in story_hits],
            "note": ("No stories matched. Stories are only written where this "
                     "account's topics matched, so try search_transcripts, "
                     "which covers everything that was said."
                     if not story_hits else ""),
        }

    # search_transcripts is all that remains; the name was validated above.
    transcript_hits = _search_transcripts(
        storage_root, query, channel=channel, since=since, until=until,
        limit=limit, allowed_channel_ids=allowed_channel_ids)
    return {"query": query, "count": len(transcript_hits),
            "results": [h.as_dict(base_url) for h in transcript_hits]}


def handle_rpc(message: dict, *, authenticate: Callable[[], tuple | None],
               storage_root, base_url: str) -> Response | None:
    """Handle one JSON-RPC message. Returns None for a notification."""
    req_id = message.get("id")
    method = message.get("method")
    is_notification = "id" not in message

    if message.get("jsonrpc") != "2.0" or not isinstance(method, str):
        return _error(req_id, _INVALID_REQUEST, "Not a JSON-RPC 2.0 request")

    if method in ("notifications/initialized", "notifications/cancelled"):
        return None

    if method == "initialize":
        return _result(req_id, {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": "tubenews", "version": "1.0.0"},
            "instructions": _SERVER_INSTRUCTIONS,
        })

    if method == "ping":
        return _result(req_id, {})

    if method == "tools/list":
        return _result(req_id, {"tools": TOOLS})

    if method == "tools/call":
        identity = authenticate()
        if identity is None:
            return _error(req_id, _INVALID_REQUEST,
                          "Invalid or revoked API key", http_status=401)
        user_data, user_id, key_record = identity
        params = message.get("params") or {}
        name = str(params.get("name") or "")
        args = params.get("arguments") or {}
        if not isinstance(args, dict):
            return _error(req_id, _INVALID_PARAMS, "arguments must be an object")

        allowed = set(user_data.get("channels", {}).keys())
        logger.info(
            "MCP: user=%s key=%s tool=%s args=%s",
            user_id, key_record.get("id", "?"), name,
            json.dumps(args, ensure_ascii=False)[:200],
        )
        try:
            payload = run_tool(name, args, storage_root=storage_root,
                               allowed_channel_ids=allowed, user_id=user_id,
                               base_url=base_url)
        except KeyError:
            return _error(req_id, _METHOD_NOT_FOUND, f"Unknown tool: {name}")
        except ValueError as exc:
            return _result(req_id, _tool_error(str(exc)))
        except Exception as exc:  # pragma: no cover - defensive
            logger.exception("MCP: tool %s failed", name)
            return _result(req_id, _tool_error(f"Search failed: {exc}"))
        return _result(req_id, _tool_result(payload))

    if is_notification:
        return None
    return _error(req_id, _METHOD_NOT_FOUND, f"Unknown method: {method}")
