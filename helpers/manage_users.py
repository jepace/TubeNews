#!/usr/bin/env python3
"""manage_users.py — Interactively create and edit TubeNews user feeds.

Each user's config is stored in archive/users/<slug>/user.json.
Run TubeNews.py after making changes to regenerate the user's RSS feed.

Usage:
    python helpers/manage_users.py
"""

import json
import re
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
CONFIG_FILE = BASE_DIR / "TubeNews.json"

try:
    _archive_dir = json.loads(CONFIG_FILE.read_text()).get("archive_dir", "")
    if _archive_dir:
        _p = Path(_archive_dir)
        STORAGE_ROOT = _p if _p.is_absolute() else (BASE_DIR / _p).resolve()
    else:
        STORAGE_ROOT = BASE_DIR / "archive"
except Exception:
    STORAGE_ROOT = BASE_DIR / "archive"
USERS_ROOT = STORAGE_ROOT / "users"


def slugify(text: str) -> str:
    return re.sub(r"[^a-zA-Z0-9]", "_", text).strip("_")


def load_config() -> dict:
    if not CONFIG_FILE.exists():
        print(f"Error: {CONFIG_FILE} not found. Copy TubeNews.json.sample first.")
        sys.exit(1)
    return json.loads(CONFIG_FILE.read_text())


def list_channels(config: dict) -> list[dict]:
    return config.get("feeds", [])


def load_user(user_dir: Path) -> dict:
    return json.loads((user_dir / "user.json").read_text())


def save_user(user_dir: Path, user: dict) -> None:
    user_dir.mkdir(parents=True, exist_ok=True)
    (user_dir / "user.json").write_text(json.dumps(user, indent=2))


def list_users() -> list[Path]:
    if not USERS_ROOT.is_dir():
        return []
    return sorted(p for p in USERS_ROOT.iterdir() if (p / "user.json").exists())


def print_channels(channels: list[dict], selected_ids: set[str] | None = None) -> None:
    for i, ch in enumerate(channels, 1):
        marker = ""
        if selected_ids is not None:
            marker = " [x]" if ch["channel_id"] in selected_ids else " [ ]"
        print(f"  {i}.{marker} {ch['channel_name']} ({ch['channel_id']})")


def pick_channels(channels: list[dict], current_ids: set[str]) -> set[str]:
    """Toggle channel selection until the user confirms."""
    selected = set(current_ids)
    while True:
        print("\nChannels (toggle by number, blank to confirm):")
        print_channels(channels, selected)
        raw = input("  Toggle #: ").strip()
        if raw == "":
            return selected
        try:
            idx = int(raw) - 1
            ch_id = channels[idx]["channel_id"]
            if ch_id in selected:
                selected.discard(ch_id)
            else:
                selected.add(ch_id)
        except (ValueError, IndexError):
            print("  Invalid number.")


def add_user(config: dict) -> None:
    channels = list_channels(config)
    if not channels:
        print("No channels configured in TubeNews.json.")
        return

    name = input("New user name: ").strip()
    if not name:
        print("Cancelled.")
        return

    user_dir = USERS_ROOT / slugify(name)
    if (user_dir / "user.json").exists():
        print(f"User '{name}' already exists. Use edit instead.")
        return

    selected = pick_channels(channels, set())
    if not selected:
        print("No channels selected — user not created.")
        return

    user = {"name": name, "channel_ids": sorted(selected)}
    save_user(user_dir, user)
    print(f"Created user '{name}' → {user_dir / 'user.json'}")
    print("Run TubeNews.py to generate the RSS feed.")


def edit_user(config: dict) -> None:
    users = list_users()
    if not users:
        print("No users found.")
        return

    channels = list_channels(config)
    print("\nUsers:")
    for i, u in enumerate(users, 1):
        data = load_user(u)
        print(f"  {i}. {data['name']} ({len(data.get('channel_ids', []))} channels)")

    raw = input("Edit #: ").strip()
    try:
        user_dir = users[int(raw) - 1]
    except (ValueError, IndexError):
        print("Invalid selection.")
        return

    user = load_user(user_dir)
    print(f"\nEditing: {user['name']}")
    selected = pick_channels(channels, set(user.get("channel_ids", [])))
    user["channel_ids"] = sorted(selected)
    save_user(user_dir, user)
    print(f"Saved. Run TubeNews.py to regenerate the RSS feed.")


def remove_user() -> None:
    users = list_users()
    if not users:
        print("No users found.")
        return

    print("\nUsers:")
    for i, u in enumerate(users, 1):
        data = load_user(u)
        print(f"  {i}. {data['name']}")

    raw = input("Remove #: ").strip()
    try:
        user_dir = users[int(raw) - 1]
    except (ValueError, IndexError):
        print("Invalid selection.")
        return

    user = load_user(user_dir)
    confirm = input(f"Remove '{user['name']}'? [y/N]: ").strip().lower()
    if confirm != "y":
        print("Cancelled.")
        return

    for f in user_dir.iterdir():
        f.unlink()
    user_dir.rmdir()
    print(f"Removed user '{user['name']}'.")


def show_users() -> None:
    users = list_users()
    if not users:
        print("No users found. Use [a] to add one.")
        return
    print("\nCurrent users:")
    for u in users:
        data = load_user(u)
        ids = data.get("channel_ids", [])
        print(f"  {data['name']}  ({len(ids)} channel{'s' if len(ids) != 1 else ''})")
        for ch_id in ids:
            print(f"    - {ch_id}")


def main() -> None:
    config = load_config()

    while True:
        print("\n--- TubeNews User Manager ---")
        show_users()
        print("\n[a] Add user  [e] Edit user  [r] Remove user  [q] Quit")
        choice = input("Choice: ").strip().lower()
        if choice == "a":
            add_user(config)
        elif choice == "e":
            edit_user(config)
        elif choice == "r":
            remove_user()
        elif choice == "q":
            break
        else:
            print("Unknown option.")


if __name__ == "__main__":
    main()
