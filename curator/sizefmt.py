"""``format_bytes`` — human sizes for the snapshot listing."""

from __future__ import annotations


def format_bytes(n) -> str:
    """1234567 -> '1.2 MB'; ``"?"`` for None / unparseable input."""
    try:
        size = float(n)
    except (TypeError, ValueError):
        return "?"
    if size < 1024:
        return f"{int(size)} B"
    for unit in ("KB", "MB", "GB"):
        size /= 1024.0
        if size < 1024:
            return f"{size:.1f} {unit}"
    return f"{size / 1024.0:.1f} TB"
