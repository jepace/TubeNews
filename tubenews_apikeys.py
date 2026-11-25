"""Per-user API keys for the MCP endpoint.

Keys are what make a network-reachable MCP endpoint safe to offer in a
multiuser install: every request is attributable to one user and one key, so
a misbehaving agent can be rate-limited, found in the log and revoked
without touching anyone else's access.

Storage is a list under ``api_keys`` in ``state/users/<uuid>/user.json``,
alongside everything else about that user. Only a **hash** of the secret is
stored. The plaintext is shown once, at creation, and cannot be recovered —
a store that can show a key back is a store that leaks every key at once if
it is read.

The secret is 32 random bytes, so a single unsalted SHA-256 is the right
hash here: there is nothing to brute-force and nothing to pre-compute, and a
slow KDF would be paid on every MCP request for no gain. That reasoning does
**not** carry over to passwords, which stay on werkzeug's hasher.
"""
from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timezone

# Visible prefix so a leaked key is recognisable in a log or a paste, and so
# a client sending something else can be told what is wrong.
KEY_PREFIX = "tnk_"

# What a key may reach. A key records the scope it was granted, but an
# archive-wide one is only honoured while its owner is *still* an admin —
# see the enforcement in web/app.py. Storing the grant without re-checking
# would leave a demoted admin holding the whole archive.
SCOPE_SUBSCRIBED = "subscribed"   # only the owner's subscribed channels
SCOPE_ALL = "all"                 # the entire archive; admins only
_SCOPES = (SCOPE_SUBSCRIBED, SCOPE_ALL)
_KEY_BYTES = 32
_MAX_KEYS_PER_USER = 10
_MAX_LABEL = 60


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def hash_key(secret: str) -> str:
    """Hash a key for storage or comparison."""
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def generate_key() -> tuple[str, str, str]:
    """Return ``(key_id, plaintext_secret, hashed_secret)`` for a new key.

    The id is public and safe to log or show in a UI; the plaintext is
    returned only here, to be displayed once.
    """
    key_id = secrets.token_hex(8)
    secret = KEY_PREFIX + secrets.token_urlsafe(_KEY_BYTES)
    return key_id, secret, hash_key(secret)


def list_keys(user_data: dict) -> list[dict]:
    """Return this user's keys, newest first. Never includes a secret."""
    keys = [k for k in user_data.get("api_keys", []) if isinstance(k, dict)]
    return sorted(keys, key=lambda k: str(k.get("created", "")), reverse=True)


def active_keys(user_data: dict) -> list[dict]:
    return [k for k in list_keys(user_data) if not k.get("revoked")]


def key_scope(record: dict) -> str:
    """The scope recorded on *record*, defaulting to the narrow one.

    Anything unrecognised reads as ``SCOPE_SUBSCRIBED``: a key whose scope
    cannot be understood must not be treated as the privileged kind.
    """
    scope = str(record.get("scope", SCOPE_SUBSCRIBED))
    return scope if scope in _SCOPES else SCOPE_SUBSCRIBED


def add_key(user_data: dict, label: str,
            scope: str = SCOPE_SUBSCRIBED) -> tuple[dict, str] | None:
    """Create a key on *user_data* in place. Returns ``(record, secret)``.

    *scope* is recorded, not authorised — the caller decides whether this
    user may be granted ``SCOPE_ALL``, and the request path re-checks it.

    Returns ``None`` when the user already holds ``_MAX_KEYS_PER_USER``
    active keys — an unbounded list is both a UI problem and a quiet way for
    one account to mint keys faster than anyone would notice.
    """
    if len(active_keys(user_data)) >= _MAX_KEYS_PER_USER:
        return None
    key_id, secret, hashed = generate_key()
    record = {
        "id": key_id,
        "label": (label or "").strip()[:_MAX_LABEL] or "API key",
        "hash": hashed,
        "scope": scope if scope in _SCOPES else SCOPE_SUBSCRIBED,
        "created": _now(),
        "last_used": "",
        "use_count": 0,
        "revoked": False,
    }
    user_data.setdefault("api_keys", []).append(record)
    return record, secret


def revoke_key(user_data: dict, key_id: str) -> bool:
    """Mark a key revoked in place. Returns whether anything changed.

    Revoked keys are kept, not deleted: the record is the only remaining
    evidence of what a key was called and when it was last used, which is
    exactly what you want after shutting one down.
    """
    for key in user_data.get("api_keys", []):
        if isinstance(key, dict) and key.get("id") == key_id and not key.get("revoked"):
            key["revoked"] = True
            key["revoked_at"] = _now()
            return True
    return False


def find_key(user_data: dict, secret: str) -> dict | None:
    """Return the active key record matching *secret*, or None.

    Comparison is constant-time against every active key, and every key is
    checked even after a match, so the time taken does not reveal which key
    matched or how many a user holds.
    """
    if not secret:
        return None
    candidate = hash_key(secret)
    found = None
    for key in user_data.get("api_keys", []):
        if not isinstance(key, dict) or key.get("revoked"):
            continue
        if secrets.compare_digest(str(key.get("hash", "")), candidate):
            found = key
    return found


def note_use(key: dict) -> None:
    """Record that *key* was just used. Caller persists the user record."""
    key["last_used"] = _now()
    try:
        key["use_count"] = int(key.get("use_count", 0)) + 1
    except (TypeError, ValueError):
        key["use_count"] = 1
