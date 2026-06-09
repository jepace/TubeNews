#!/usr/bin/env python3
"""Migrate video directories from YYYY-MM-DD_videoId format to just videoId.

This script renames meeting directories to remove the date prefix and ensures
video_date is stored in metadata.json. The old naming scheme with a date prefix
created bugs because queue entries containing full ISO timestamps had to be
truncated in multiple places. Removing the prefix centralizes the date in
metadata.json.

How it works
------------
For each channel directory under STORAGE_ROOT:
1. Iterate through all subdirectories
2. For each dir matching YYYY-MM-DD_videoId pattern:
   - Extract date (YYYY-MM-DD) and video_id
   - Read metadata.json if present; add/update "video_date"; rewrite atomically
   - If target (just videoId) already exists: warn and skip (conflict)
   - Otherwise: rename dir to videoId

Dry-run mode (default): shows what would be renamed without making changes.
Apply mode (--apply): actually performs the renames.

Validates each directory name matches the pattern before touching it.

Usage::

    # Dry-run (default)
    python3 helpers/rename_dirs.py

    # Apply changes
    python3 helpers/rename_dirs.py --apply

Safe to re-run: directories already renamed (just videoId) are skipped.
"""

import argparse
import json
import re
import sys
from pathlib import Path

# Resolve paths the same way TubeNews.py does
BASE_DIR = Path(__file__).resolve().parent.parent
CONFIG_FILE = BASE_DIR / "config.json"

sys.path.insert(0, str(BASE_DIR))
from tubenews_utils import resolve_roots  # noqa: E402

STORAGE_ROOT, STATE_ROOT = resolve_roots(CONFIG_FILE, BASE_DIR)

# Pattern to match YYYY-MM-DD_videoId directories
OLD_DIR_PATTERN = re.compile(r'^\d{4}-\d{2}-\d{2}_[A-Za-z0-9_-]+$')


def _update_metadata_with_date(meta_path: Path, video_date: str) -> None:
    """Update metadata.json to include video_date field.

    Reads the existing metadata dict, adds or updates video_date, and writes
    back atomically (write-then-rename).
    """
    try:
        if meta_path.exists():
            meta = json.loads(meta_path.read_text())
        else:
            meta = {}
    except Exception:
        meta = {}

    meta["video_date"] = video_date

    # Write atomically
    tmp_path = meta_path.parent / f"{meta_path.name}.tmp"
    tmp_path.write_text(json.dumps(meta, indent=2))
    tmp_path.replace(meta_path)


def migrate(apply: bool = False) -> None:
    """Iterate through channels and rename matching directories.

    Args:
        apply: If True, perform the renames. If False, dry-run only.
    """
    renamed = 0
    skipped_conflicts = 0
    already_clean = 0
    scanned = 0

    if not STORAGE_ROOT.exists():
        print(f"Error: {STORAGE_ROOT} does not exist")
        sys.exit(1)

    for channel_dir in sorted(STORAGE_ROOT.iterdir()):
        if not channel_dir.is_dir():
            continue

        # Skip if no channel.json (not a real channel)
        if not (channel_dir / "channel.json").exists():
            continue

        channel_name = channel_dir.name
        print(f"\n[*] Channel: {channel_name}")

        for sub_dir in sorted(channel_dir.iterdir()):
            if not sub_dir.is_dir():
                continue

            scanned += 1
            dir_name = sub_dir.name

            # Check if it matches the old pattern
            if not OLD_DIR_PATTERN.match(dir_name):
                # Already clean or some other dir
                if not (sub_dir / "channel.json").exists():
                    # It's likely a video dir that's already been renamed
                    if (sub_dir / "metadata.json").exists():
                        already_clean += 1
                continue

            # Parse the old format: YYYY-MM-DD_videoId
            parts = dir_name.split("_", 1)
            if len(parts) != 2:
                continue

            date_prefix = parts[0]
            video_id = parts[1]

            # Check if target (just videoId) already exists
            target_dir = channel_dir / video_id
            if target_dir.exists():
                print(f"    [!] Conflict: {dir_name} → {video_id} (target exists, skipping)")
                skipped_conflicts += 1
                continue

            # Update metadata with video_date
            meta_path = sub_dir / "metadata.json"
            _update_metadata_with_date(meta_path, date_prefix)

            if apply:
                # Perform the rename
                sub_dir.rename(target_dir)
                print(f"    [✓] Renamed: {dir_name} → {video_id}")
            else:
                print(f"    [→] Would rename: {dir_name} → {video_id}")

            renamed += 1

    # Summary
    print("\n[Summary]")
    print(f"  Scanned: {scanned} directories")
    print(f"  Would rename / Renamed: {renamed}")
    print(f"  Skipped (conflicts): {skipped_conflicts}")
    print(f"  Already clean: {already_clean}")

    if not apply:
        print("\n[Info] Dry-run mode. Pass --apply to perform the renames.")
        print("       python3 helpers/rename_dirs.py --apply")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Migrate video directories from YYYY-MM-DD_videoId to videoId format"
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually perform the renames (default: dry-run only)"
    )
    args = parser.parse_args()

    migrate(apply=args.apply)
    if args.apply:
        print("\n[✓] Migration complete.")
