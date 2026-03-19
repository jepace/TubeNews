#!/usr/bin/env python3
"""Reset a TubeNews user's password from the command line.

Usage:
    python helpers/reset_password.py

Run from the TubeNews project root.  No arguments needed — the script
prompts for the user's email and the new password interactively.
"""

import getpass
import json
import sys
from pathlib import Path

from werkzeug.security import generate_password_hash

BASE_DIR = Path(__file__).resolve().parent.parent
USERS_ROOT = BASE_DIR / "archive" / "users"


def find_user(email: str):
    """Return (user_json_path, data) for the given email, or (None, None)."""
    if not USERS_ROOT.is_dir():
        return None, None
    needle = email.strip().lower()
    for user_json in USERS_ROOT.glob("*/user.json"):
        try:
            data = json.loads(user_json.read_text())
            if data.get("email", "").lower() == needle:
                return user_json, data
        except Exception:
            continue
    return None, None


def main():
    print("TubeNews — Password Reset")
    print("-" * 30)

    email = input("User email: ").strip().lower()
    if not email:
        print("No email entered. Aborting.")
        sys.exit(1)

    user_json, data = find_user(email)
    if user_json is None:
        print(f"No account found for '{email}'.")
        sys.exit(1)

    print(f"Found account: {data.get('name', email)} <{email}>")

    new_pw = getpass.getpass("New password (min 10 chars): ")
    if len(new_pw) < 10:
        print("Password must be at least 10 characters. Aborting.")
        sys.exit(1)

    confirm = getpass.getpass("Confirm new password: ")
    if new_pw != confirm:
        print("Passwords do not match. Aborting.")
        sys.exit(1)

    data["password_hash"] = generate_password_hash(new_pw)
    user_json.write_text(json.dumps(data, indent=2))
    print(f"Password updated for {email}.")


if __name__ == "__main__":
    main()
