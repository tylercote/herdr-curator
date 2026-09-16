"""curator.curator — per-run report writer (run.json + REPORT.md),
and the activity-timestamp regression (ports of test_curator_reports.py / test_curator_activity.py)."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from conftest import write_skill


def _make_llm_meta(**overrides):
    base = {"final": "short summary of the pass", "summary": "short summary", "model": "test-model",
            "provider": "test-provider", "tool_calls": [], "error": None}
    base.update(overrides)
    return base


def _empty_counts():
    return {"checked": 0, "marked_stale": 0, "archived": 0, "reactivated": 0}


def test_write_run_report_creates_both_files(home):
    from curator import curator
    run_dir = curator._write_run_report(
        started_at=datetime.now(timezone.utc), elapsed_seconds=12.345,
        auto_counts={"checked": 5, "marked_stale": 1, "archived": 0, "reactivated": 0}, auto_summary="1 marked stale",
        before_report=[], before_names=set(), after_report=[], llm_meta=_make_llm_meta())
    assert run_dir is not None and run_dir.is_dir()
    assert (run_dir / "run.json").exists() and (run_dir / "REPORT.md").exists()
    assert run_dir.parent == curator._reports_root() == home / "logs" / "curator"
    payload = json.loads((run_dir / "run.json").read_text())
    assert payload["duration_seconds"] == 12.35 and payload["auto_transitions"]["marked_stale"] == 1
    md = (run_dir / "REPORT.md").read_text()
    assert md.startswith("# Curator run — ")
    assert "Model: `test-model` via `test-provider`  ·  Duration: 12s" in md
    assert "- marked stale: 1" in md and "## Recovery" in md and "curator restore <name>" in md
    assert "## LLM final summary" in md and "short summary of the pass" in md


def test_report_unresolved_model_and_error_and_summary_fallback(home):
    from curator import curator
    run_dir = curator._write_run_report(
        started_at=datetime.now(timezone.utc), elapsed_seconds=125, auto_counts=_empty_counts(), auto_summary="x",
        before_report=[], before_names=set(), after_report=[],
        llm_meta=_make_llm_meta(final="", summary="skipped (no candidates)", model="", provider="", error="boom"))
    md = (run_dir / "REPORT.md").read_text()
    assert "Model: `(not resolved)` via `(not resolved)`  ·  Duration: 2m 5s" in md
    assert "> ⚠ LLM pass error: `boom`" in md
    assert "## LLM summary" not in md  # error suppresses the summary fallback
    run_dir2 = curator._write_run_report(
        started_at=datetime.now(timezone.utc), elapsed_seconds=0, auto_counts=_empty_counts(), auto_summary="x",
        before_report=[], before_names=set(), after_report=[],
        llm_meta=_make_llm_meta(final="", summary="skipped (consolidation off)", error=None))
    assert "## LLM summary\n\nskipped (consolidation off)" in (run_dir2 / "REPORT.md").read_text()


def test_same_second_reruns_get_unique_dirs(home):
    from curator import curator
    start = datetime(2026, 4, 29, 5, 33, 34, tzinfo=timezone.utc)
    kwargs = dict(started_at=start, elapsed_seconds=1.0, auto_counts=_empty_counts(), auto_summary="no changes",
                  before_report=[], before_names=set(), after_report=[], llm_meta=_make_llm_meta())
    a = curator._write_run_report(**kwargs)
    b = curator._write_run_report(**kwargs)
    assert a != b and a.name == "20260429-053334" and b.name == "20260429-053334-2"


def test_report_state_transitions_and_added_sections(home):
    from curator import curator
    before = [{"name": "a", "state": "active"}, {"name": "b", "state": "stale"}]
    after = [{"name": "a", "state": "stale"}, {"name": "b", "state": "stale"}, {"name": "c", "state": "active"}]
    run_dir = curator._write_run_report(
        started_at=datetime.now(timezone.utc), elapsed_seconds=1, auto_counts=_empty_counts(), auto_summary="x",
        before_report=before, before_names={"a", "b"}, after_report=after,
        llm_meta=_make_llm_meta(tool_calls=[{"name": "skill_view", "arguments": "{}"}, {"name": "skill_view", "arguments": "{}"},
                                            {"name": "skill_manage", "arguments": "{}"}]))
    payload = json.loads((run_dir / "run.json").read_text())
    assert payload["state_transitions"] == [{"name": "a", "from": "active", "to": "stale"}]
    assert payload["added"] == ["c"] and payload["counts"]["added_this_run"] == 1
    assert payload["tool_call_counts"] == {"skill_manage": 1, "skill_view": 2}
    assert payload["counts"]["tool_calls_total"] == 3
    md = (run_dir / "REPORT.md").read_text()
    assert "### State transitions (1)" in md and "- `a`: active → stale" in md
    assert "### New skills this run (1)" in md and "- `c`" in md
    assert "tool calls: **3** (by name: skill_manage=1, skill_view=2)" in md


def test_recent_view_activity_prevents_false_stale_transition(home, monkeypatch):
    from curator import curator, skill_usage
    write_skill(home / "skills", "recently-viewed")
    now = datetime(2026, 4, 30, tzinfo=timezone.utc)
    skill_usage.save_usage({"recently-viewed": {
        "created_by": "agent", "created_at": (now - timedelta(days=60)).isoformat(),
        "last_viewed_at": (now - timedelta(days=1)).isoformat(), "view_count": 1, "state": "active"}})
    monkeypatch.setattr(curator, "get_stale_after_days", lambda: 30)
    monkeypatch.setattr(curator, "get_archive_after_days", lambda: 90)
    counts = curator.apply_automatic_transitions(now=now)
    assert counts["marked_stale"] == 0
    assert skill_usage.get_record("recently-viewed")["state"] == "active"
