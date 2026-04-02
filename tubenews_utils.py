"""Lightweight utilities shared between TubeNews.py and helper scripts.

Kept dependency-free so that helper scripts (e.g. helpers/catchup.py) can
import from here without dragging in feedgen, supadata, or other heavy
third-party packages.
"""
import json
import re
from pathlib import Path


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


def resolve_roots(config_file: Path, base_dir: Path) -> tuple[Path, Path]:
    """Return ``(STORAGE_ROOT, STATE_ROOT)`` from *config_file*.

    Reads ``content_dir`` and ``state_dir`` from the JSON config.  Relative
    paths are resolved relative to *base_dir*; absolute paths are used as-is.
    Falls back to ``base_dir/content`` and ``base_dir/state`` on any error or
    when the keys are absent.

    Args:
        config_file: Path to ``TubeNews.json``.
        base_dir:    Directory used to resolve relative paths (typically the
                     project root, i.e. the parent of ``TubeNews.py``).

    Returns:
        A ``(STORAGE_ROOT, STATE_ROOT)`` tuple of resolved :class:`Path` objects.
    """
    try:
        cfg = json.loads(config_file.read_text())

        def _resolve(key: str, default: str) -> Path:
            val = cfg.get(key, "")
            if val:
                p = Path(val)
                return p if p.is_absolute() else (base_dir / p).resolve()
            return base_dir / default

        return _resolve("content_dir", "content"), _resolve("state_dir", "state")
    except Exception:
        return base_dir / "content", base_dir / "state"
