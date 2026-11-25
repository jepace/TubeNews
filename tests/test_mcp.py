"""Tests for per-user API keys and the MCP endpoint.

Run with:  pytest tests/test_mcp.py -v
"""
import json
import os
import sys
import uuid
from pathlib import Path

import pytest

os.environ.setdefault("TUBENEWS_SECRET_KEY", "test-secret-key-32-bytes-xxxxxxxx")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import tubenews_apikeys as apikeys  # noqa: E402
import web.app as webapp  # noqa: E402


# ---------------------------------------------------------------------------
# Key store
# ---------------------------------------------------------------------------

def test_the_secret_is_never_stored():
    """Only a hash is kept, so reading user.json cannot yield a usable key."""
    data = {}
    record, secret = apikeys.add_key(data, "laptop")
    stored = json.dumps(data)
    assert secret not in stored
    assert record["hash"] == apikeys.hash_key(secret)
    assert "secret" not in record


def test_key_has_a_recognisable_prefix():
    """A leaked key should be identifiable on sight in a log or a paste."""
    _, secret, _ = apikeys.generate_key()
    assert secret.startswith(apikeys.KEY_PREFIX)


def test_keys_are_unique():
    seen = {apikeys.generate_key()[1] for _ in range(50)}
    assert len(seen) == 50


def test_find_key_matches_only_the_right_secret():
    data = {}
    _, secret = apikeys.add_key(data, "a")
    assert apikeys.find_key(data, secret) is not None
    assert apikeys.find_key(data, secret + "x") is None
    assert apikeys.find_key(data, "") is None


def test_revoked_keys_stop_working_but_are_kept():
    """The record is the evidence of what the key was and when it was used."""
    data = {}
    record, secret = apikeys.add_key(data, "laptop")
    assert apikeys.revoke_key(data, record["id"]) is True
    assert apikeys.find_key(data, secret) is None
    assert len(apikeys.list_keys(data)) == 1
    assert apikeys.revoke_key(data, record["id"]) is False, "revoking twice is a no-op"


def test_active_key_count_is_capped():
    """An account cannot mint keys without bound."""
    data = {}
    for i in range(apikeys._MAX_KEYS_PER_USER):
        assert apikeys.add_key(data, f"k{i}") is not None
    assert apikeys.add_key(data, "one too many") is None
    # Revoking frees a slot.
    apikeys.revoke_key(data, apikeys.active_keys(data)[0]["id"])
    assert apikeys.add_key(data, "now fits") is not None


def test_note_use_counts_and_timestamps():
    data = {}
    record, _ = apikeys.add_key(data, "a")
    apikeys.note_use(record)
    apikeys.note_use(record)
    assert record["use_count"] == 2
    assert record["last_used"]


# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------

@pytest.fixture
def mcp(tmp_path, monkeypatch):
    """A client, a subscribed channel and an unsubscribed one."""
    content = tmp_path / "content"
    for slug, channel_id, video_id in (
        ("county_of_monterey", "UC_mont", "abc123"),
        ("secret_city", "UC_secret", "zzz999"),
    ):
        meeting = content / slug / video_id
        meeting.mkdir(parents=True)
        (content / slug / "channel.json").write_text(json.dumps(
            {"channel_name": slug.replace("_", " "), "channel_id": channel_id}))
        (meeting / "metadata.json").write_text(json.dumps({
            "video_id": video_id, "video_date": "2026-10-14",
            "processed_at": "2026-10-14T17:00:00Z"}))
        (meeting / "01.md").write_text(
            f"# Housing Element Update in {slug}\n*SALINAS*\n\n"
            "Body about the housing element.\n\n---\n**Segment Start:** 10s\n")
        (meeting / "transcript.txt").write_text(
            "1200s --> discussion of the culvert replacement\n")

    users = tmp_path / "users"
    user_id = str(uuid.uuid4())
    (users / user_id).mkdir(parents=True)
    (users / user_id / "user.json").write_text(json.dumps({
        "id": user_id, "email": "j@example.com", "name": "J",
        "feed_token": "tok", "channels": {"UC_mont": []}, "is_verified": True}))

    monkeypatch.setattr(webapp, "STORAGE_ROOT", content)
    monkeypatch.setattr(webapp, "USERS_ROOT", users)
    # Via monkeypatch so it is restored: setting this directly leaked into
    # the login rate-limit test and made it pass vacuously.
    monkeypatch.setattr(webapp.limiter, "enabled", False)

    user = webapp._find_user_by_id(user_id)
    record, secret = apikeys.add_key(user._data, "laptop")
    user._save()

    client = webapp.app.test_client()

    def rpc(method, params=None, token=secret, req_id=1):
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        response = client.post("/mcp", headers=headers, json={
            "jsonrpc": "2.0", "id": req_id, "method": method,
            "params": params or {}})
        return response

    def call(name, **arguments):
        return rpc("tools/call", {"name": name, "arguments": arguments})

    return {"rpc": rpc, "call": call, "secret": secret, "record": record,
            "user_id": user_id, "client": client, "content": content}


def _structured(response):
    return response.get_json()["result"]["structuredContent"]


def test_initialize_and_tools_list_need_no_key(mcp):
    """Discovery is open; only tools/call touches data."""
    info = mcp["rpc"]("initialize", token=None).get_json()["result"]
    assert info["serverInfo"]["name"] == "tubenews"
    names = [t["name"] for t in
             mcp["rpc"]("tools/list", token=None).get_json()["result"]["tools"]]
    assert names == ["search_stories", "search_transcripts",
                     "get_story", "list_channels"]


def test_tool_call_without_a_key_is_rejected(mcp):
    assert mcp["rpc"]("tools/call", {"name": "list_channels"},
                      token=None).status_code == 401


def test_tool_call_with_an_unknown_key_is_rejected(mcp):
    assert mcp["rpc"]("tools/call", {"name": "list_channels"},
                      token="tnk_not_a_real_key").status_code == 401


def test_a_revoked_key_stops_working_immediately(mcp):
    """The point of the whole design: shut one agent down without touching others."""
    assert mcp["call"]("list_channels").status_code == 200
    user = webapp._find_user_by_id(mcp["user_id"])
    apikeys.revoke_key(user._data, mcp["record"]["id"])
    user._save()
    assert mcp["call"]("list_channels").status_code == 401


def test_results_are_limited_to_subscribed_channels(mcp):
    """A key sees what the account sees — not the whole archive."""
    channels = [c["channel"] for c in _structured(mcp["call"]("list_channels"))["channels"]]
    assert channels == ["county of monterey"]

    titles = [r["title"] for r in
              _structured(mcp["call"]("search_stories", query="housing element"))["results"]]
    assert titles == ["Housing Element Update in county_of_monterey"]
    assert all("secret" not in t for t in titles)

    hits = _structured(mcp["call"]("search_transcripts", query="culvert"))["results"]
    assert [h["channel"] for h in hits] == ["county of monterey"]


def test_get_story_cannot_reach_an_unsubscribed_channel(mcp):
    """Scoping is enforced on fetch too, not only on search.

    Otherwise an id seen elsewhere would retrieve what search had hidden.
    """
    payload = _structured(mcp["call"]("get_story", video_id="zzz999", start_seconds=10))
    assert "error" in payload
    assert "subscribes" in payload["error"]


def test_a_user_with_no_subscriptions_sees_nothing(mcp):
    """An empty subscription set must mean nothing, not everything."""
    user = webapp._find_user_by_id(mcp["user_id"])
    user._data["channels"] = {}
    user._save()
    assert _structured(mcp["call"]("list_channels"))["channels"] == []
    assert _structured(mcp["call"]("search_stories", query="housing"))["count"] == 0


def test_story_attribution_is_respected(mcp):
    """A story written for another user is not in this user's results."""
    story = mcp["content"] / "county_of_monterey" / "abc123" / "01.md"
    story.write_text(story.read_text() + "\n**Users:** someone-else\n")
    assert _structured(mcp["call"]("search_stories", query="housing element"))["count"] == 0


def test_using_a_key_records_it_for_attribution(mcp):
    """Abuse has to be traceable to a key, so each call is counted."""
    mcp["call"]("list_channels")
    record = apikeys.list_keys(webapp._find_user_by_id(mcp["user_id"])._data)[0]
    assert record["use_count"] >= 1
    assert record["last_used"]


def test_a_missing_query_is_a_tool_error_not_a_crash(mcp):
    result = mcp["call"]("search_stories").get_json()["result"]
    assert result["isError"] is True


def test_an_unknown_tool_is_reported(mcp):
    body = mcp["call"]("no_such_tool").get_json()
    assert body["error"]["code"] == -32601


def test_limit_is_clamped(mcp):
    """A client cannot ask for an unbounded result set."""
    payload = _structured(mcp["call"]("search_stories", query="housing", limit=10_000))
    assert payload["count"] <= 50


def test_notifications_get_no_body(mcp):
    response = mcp["client"].post(
        "/mcp", json={"jsonrpc": "2.0", "method": "notifications/initialized"})
    assert response.status_code == 202
    assert response.data == b""


def test_malformed_json_is_a_parse_error(mcp):
    response = mcp["client"].post("/mcp", data="{not json",
                                  content_type="application/json")
    assert response.status_code == 400
    assert response.get_json()["error"]["code"] == -32700


def test_endpoint_is_csrf_exempt(mcp):
    """A bearer API must not require a browser CSRF token."""
    assert mcp["call"]("list_channels").status_code == 200


def test_rate_limit_key_is_per_key_not_per_address(mcp):
    """One runaway agent must not spend everyone else's budget.

    Limiting by IP would also throttle everyone behind the same NAT, and
    would not restrain an agent that moves between addresses.
    """
    from web.mcp_api import rate_limit_key
    with webapp.app.test_request_context(
            "/mcp", headers={"Authorization": f"Bearer {mcp['secret']}"}):
        keyed = rate_limit_key()
    with webapp.app.test_request_context("/mcp"):
        anonymous = rate_limit_key()
    assert keyed.startswith("mcpkey:")
    assert mcp["secret"] not in keyed, "the limiter must not hold the secret"
    assert anonymous.startswith("mcpaddr:")
