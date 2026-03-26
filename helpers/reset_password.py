#!/usr/bin/env python3
"""Reset a TubeNews user's password from the command line.

Use this when an admin has forgotten their own password and can no longer
log in to use the web admin panel.  For any other user, prefer the admin
panel (``/admin/user/<uid>/password``) — it requires no shell access.

Usage::

    python3 helpers/reset_password.py

The script prompts for the user's email address and the new password
interactively (the password is not echoed to the terminal).

Requirements
------------
* Must be run on the same machine that hosts the TubeNews archive.
* ``werkzeug`` must be installed (it is a standard TubeNews dependency).
* The new password must be at least 10 characters.
"""

import getpass
import json
import sys
from pathlib import Path

from werkzeug.security import generate_password_hash

BASE_DIR = Path(__file__).resolve().parent.parent
USERS_ROOT = BASE_DIR / "archive" / "_users"


def find_user(email: str) -> tuple[Path, dict] | tuple[None, None]:
    """Locate a user by email address and return their JSON file path and data.

    Checks ``_users/index.json`` first for an O(1) lookup, then falls back
    to a full glob scan if the index is missing or stale (e.g. on older
    installs that pre-date the index).

    Args:
        email: The account's email address (case-insensitive).

    Returns:
        ``(path_to_user.json, data_dict)`` if found, otherwise ``(None, None)``.
    """
    if not USERS_ROOT.is_dir():
        return None, None

    needle = email.strip().lower()

    # Fast path: O(1) index lookup.
    index_file = USERS_ROOT / "index.json"
    if index_file.exists():
        try:
            index = json.loads(index_file.read_text())
            uid = index.get(needle)
            if uid:
                user_json = USERS_ROOT / uid / "user.json"
                if user_json.exists():
                    data = json.loads(user_json.read_text())
                    if data.get("email", "").lower() == needle:
                        return user_json, data
        except Exception:
            pass  # Fall through to glob scan on any index error.

    # Slow path: glob scan (pre-index installs or stale index).
    for user_json in USERS_ROOT.glob("*/user.json"):
        try:
            data = json.loads(user_json.read_text())
            if data.get("email", "").lower() == needle:
                return user_json, data
        except Exception:
            continue

    return None, None


def main() -> None:
    print("TubeNews — Password Reset")
    print("-" * 30)

    email = input("User email: ").strip().lower()
    if not email:
        sys.exit("No email entered. Aborting.")

    user_json, data = find_user(email)
    if user_json is None:
        sys.exit(f"No account found for '{email}'.")

    print(f"Found account: {data.get('name', email)} <{email}>")

    new_pw = getpass.getpass("New password (min 10 chars): ")
    if len(new_pw) < 10:
        sys.exit("Password must be at least 10 characters. Aborting.")

    confirm = getpass.getpass("Confirm new password: ")
    if new_pw != confirm:
        sys.exit("Passwords do not match. Aborting.")

    data["password_hash"] = generate_password_hash(new_pw)

    # Write atomically so a crash mid-write cannot corrupt the user file.
    tmp = user_json.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2))
    tmp.replace(user_json)

    print(f"Password updated for {email}.")
    print("The user can now log in at the TubeNews web UI.")


if __name__ == "__main__":
    main()
