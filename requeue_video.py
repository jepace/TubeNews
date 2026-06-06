#!/usr/bin/env python3
"""Utility to manually add a video to the processing queue.

Usage:
    python3 requeue_video.py <video_id_or_url> [channel_id]

Examples:
    python3 requeue_video.py dQw4w9WgXcQ
    python3 requeue_video.py https://youtu.be/dQw4w9WgXcQ
    python3 requeue_video.py dQw4w9WgXcQ UCxxxxxxxxxxxxxxxxxxxxxxx
"""

import json
import sys
import re
from pathlib import Path
from datetime import datetime, timezone


def extract_video_id(url_or_id: str) -> str:
    """Extract YouTube video ID from URL or return as-is if already an ID."""
    if url_or_id.startswith(('http://', 'https://')):
        # Try common YouTube URL patterns
        patterns = [
            r'youtube\.com/watch\?.*v=([a-zA-Z0-9_-]{11})',
            r'youtu\.be/([a-zA-Z0-9_-]{11})',
            r'youtube\.com/embed/([a-zA-Z0-9_-]{11})',
        ]
        for pattern in patterns:
            match = re.search(pattern, url_or_id)
            if match:
                return match.group(1)
        raise ValueError(f"Could not extract video ID from URL: {url_or_id}")
    elif re.match(r'^[a-zA-Z0-9_-]{11}$', url_or_id):
        return url_or_id
    else:
        raise ValueError(f"Invalid video ID format: {url_or_id}")


def load_channels() -> list[dict]:
    """Load channels from state/channels.json."""
    channels_file = Path(__file__).parent / "state" / "channels.json"
    if not channels_file.exists():
        return []
    try:
        return json.loads(channels_file.read_text())
    except Exception as e:
        print(f"Warning: Could not load channels.json: {e}")
        return []


def find_channel_id(channels: list[dict]) -> str:
    """Interactively select a channel, or return first enabled channel."""
    enabled = [ch for ch in channels if not ch.get("disabled", False)]
    if not enabled:
        raise ValueError("No enabled channels found in state/channels.json")

    if len(enabled) == 1:
        return enabled[0]["channel_id"]

    print("\nAvailable channels:")
    for i, ch in enumerate(enabled, 1):
        print(f"  {i}. {ch['channel_name']} ({ch['channel_id']})")

    while True:
        try:
            choice = input(f"Select channel (1-{len(enabled)}): ").strip()
            idx = int(choice) - 1
            if 0 <= idx < len(enabled):
                return enabled[idx]["channel_id"]
        except ValueError:
            pass
        print("Invalid choice. Try again.")


def add_to_queue(video_id: str, channel_id: str) -> None:
    """Add video to the processing queue."""
    state_root = Path(__file__).parent / "state"
    queue_dir = state_root / "queue"
    queue_dir.mkdir(parents=True, exist_ok=True)
    queue_path = queue_dir / "push_queue.json"

    # Load existing queue
    try:
        items = json.loads(queue_path.read_text()) if queue_path.exists() else []
    except Exception as e:
        print(f"Error reading queue: {e}")
        items = []

    # Check if already in queue
    if any(item["video_id"] == video_id for item in items):
        print(f"⚠️  Video {video_id} is already in the queue")
        return

    # Create new queue entry
    now = datetime.now(timezone.utc)
    now_iso = now.isoformat(timespec="seconds").replace("+00:00", "Z")

    entry = {
        "video_id": video_id,
        "channel_id": channel_id,
        "title": "[Manually requeued]",
        "date": now_iso,
        "scheduled_start": None,
        "raw_entry_xml": "",
        "queued_at": now_iso,
        "next_try_at": now_iso,  # Process immediately
        "transcript_attempts": 0,
        "retry_count": 0,
    }

    items.append(entry)

    # Write atomically
    try:
        tmp = queue_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(items, indent=2))
        tmp.replace(queue_path)
        print(f"✓ Added video {video_id} to processing queue")
        print(f"  Channel: {channel_id}")
        print(f"  The processor will pick it up on the next cycle (~1 minute)")
    except Exception as e:
        print(f"✗ Failed to write queue: {e}")
        sys.exit(1)


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    url_or_id = sys.argv[1]

    try:
        video_id = extract_video_id(url_or_id)
    except ValueError as e:
        print(f"✗ Error: {e}")
        sys.exit(1)

    # Get channel ID from argument or interactively
    if len(sys.argv) >= 3:
        channel_id = sys.argv[2]
    else:
        channels = load_channels()
        try:
            channel_id = find_channel_id(channels)
        except ValueError as e:
            print(f"✗ Error: {e}")
            sys.exit(1)

    add_to_queue(video_id, channel_id)


if __name__ == "__main__":
    main()
