"""Lightweight utilities shared between TubeNews.py and helper scripts.

Kept dependency-free so that helper scripts (e.g. helpers/catchup.py) can
import from here without dragging in feedgen, supadata, or other heavy
third-party packages.
"""
import re


def slugify(text: str) -> str:
    """Convert *text* to a filesystem-safe slug.

    Every character that isn't a letter or digit is replaced with an
    underscore, then leading/trailing underscores are stripped.

    Examples:
        >>> slugify("City Council")
        'City_Council'
        >>> slugify("---test---")
        'test'
    """
    return re.sub(r"[^a-zA-Z0-9]", "_", text).strip("_")
