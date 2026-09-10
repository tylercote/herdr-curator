"""User-facing curator notices.

The plugin's startup hook shows them as Herdr notifications. The text helpers
return the lines so the host layer can route them anywhere.
"""

from __future__ import annotations

from contextlib import suppress
from datetime import datetime, timezone
from typing import List, Optional

from curator.prog import cmd as _cmd


def format_time_ago(iso_ts: str) -> str:
    """``Xh ago`` / ``Xd ago`` / ``Xm ago`` / ``just now``; ``recently`` when unparseable."""
    try:
        ts = datetime.fromisoformat(iso_ts.replace("Z", "+00:00"))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        secs = int((datetime.now(timezone.utc) - ts).total_seconds())
    except Exception:
        return "recently"
    if secs < 60:
        return "just now"
    if secs < 3600:
        return f"{secs // 60}m ago"
    if secs < 86400:
        return f"{secs // 3600}h ago"
    return f"{secs // 86400}d ago"


def first_run_notice_text() -> Optional[List[str]]:
    """Lines for the heads-up shown while the deferred first pass is pending, else None."""
    try:
        from curator import curator
        if not curator.is_enabled():
            return None
        state = curator.load_state()
    except Exception:
        return None
    if state.get("last_run_at"):
        return None
    try:
        hours = curator.get_interval_hours()
    except Exception:
        hours = 24 * 7
    days = max(1, hours // 24)
    return [
        "ℹ Skill curator",
        f"  Background skill maintenance is enabled. First pass is deferred ~{days}d after installation; "
        "only agent-created skills are in scope and nothing is ever auto-deleted (archive is recoverable).",
        f"  Preview now:  {_cmd('run --dry-run')}",
        f"  Pause it:     {_cmd('pause')}",
        "  Docs:         README.md / ARCHITECTURE.md in the plugin root",
    ]


def print_curator_first_run_notice() -> None:
    lines = first_run_notice_text()
    if lines:
        print()
        for line in lines:
            print(line)


def recent_run_notice_text(*, mark_shown: bool = True) -> Optional[List[str]]:
    """Lines for the latest run's rename map, once per run (stamps ``last_run_summary_shown_at``)."""
    try:
        from curator import curator
        state = curator.load_state()
    except Exception:
        return None
    last_run_at = state.get("last_run_at")
    if not last_run_at:
        return None
    if state.get("last_run_summary_shown_at") == last_run_at:
        return None
    summary = state.get("last_run_summary") or ""
    if not summary:
        return None
    lines: Optional[List[str]] = None
    if "\n" in summary:
        lines = [f"ℹ Skill curator — last run {format_time_ago(last_run_at)}"]
        lines += [f"  {line}" for line in summary.splitlines()]
        lines.append(f"  (This message shows once per curator run. View anytime: {_cmd('status')})")
    if mark_shown:
        with suppress(Exception):
            state["last_run_summary_shown_at"] = last_run_at
            curator.save_state(state)
    return lines


def print_curator_recent_run_notice() -> None:
    lines = recent_run_notice_text(mark_shown=True)
    if lines:
        print()
        for line in lines:
            print(line)
