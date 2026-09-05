"""curator.notices — first-run / recent-run notices (port of hermes_cli.update_cmd_maint curator bits)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone


def _set_state(curator_mod, **fields):
    state = curator_mod.load_state()
    state.update(fields)
    curator_mod.save_state(state)


def test_first_run_notice_only_before_first_run(home, capsys, set_config):
    from curator import curator, notices
    notices.print_curator_first_run_notice()
    out = capsys.readouterr().out
    assert "Skill curator" in out and "deferred ~7d" in out and "curator run --dry-run" in out
    _set_state(curator, last_run_at=datetime.now(timezone.utc).isoformat())
    notices.print_curator_first_run_notice()
    assert capsys.readouterr().out == ""
    set_config({"curator": {"enabled": False}})
    curator.save_state({})
    notices.print_curator_first_run_notice()
    assert capsys.readouterr().out == ""


def test_recent_run_notice_prints_multiline_once(home, capsys):
    from curator import curator, notices
    now = datetime.now(timezone.utc).isoformat()
    _set_state(curator, last_run_at=now, last_run_summary="auto: 1 archived; llm: ok\narchived 1 skill(s):\n  • a → b")
    notices.print_curator_recent_run_notice()
    out = capsys.readouterr().out
    assert "Skill curator — last run" in out and "• a → b" in out
    assert curator.load_state()["last_run_summary_shown_at"] == now
    notices.print_curator_recent_run_notice()
    assert capsys.readouterr().out == ""


def test_silent_when_summary_is_single_line(home, capsys):
    from curator import curator, notices
    now = datetime.now(timezone.utc).isoformat()
    _set_state(curator, last_run_at=now, last_run_summary="auto: no changes; llm: no change")
    notices.print_curator_recent_run_notice()
    assert "Skill curator — last run" not in capsys.readouterr().out
    assert curator.load_state()["last_run_summary_shown_at"] == now


def test_silent_when_never_run(home, capsys):
    from curator import notices
    notices.print_curator_recent_run_notice()
    assert capsys.readouterr().out == ""


def test_format_time_ago_buckets(home):
    from curator.notices import format_time_ago
    now = datetime.now(timezone.utc)
    assert format_time_ago((now - timedelta(seconds=10)).isoformat()) == "just now"
    assert format_time_ago((now - timedelta(minutes=5)).isoformat()) == "5m ago"
    assert format_time_ago((now - timedelta(hours=3)).isoformat()) == "3h ago"
    assert format_time_ago((now - timedelta(days=2)).isoformat()) == "2d ago"
    assert format_time_ago("not-a-real-iso-string") == "recently"


def test_recent_run_notice_text_helper_returns_lines(home):
    from curator import curator, notices
    now = datetime.now(timezone.utc).isoformat()
    _set_state(curator, last_run_at=now, last_run_summary="auto: x\n  • a → b")
    text = notices.recent_run_notice_text(mark_shown=False)
    assert text and "• a → b" in "\n".join(text)
    assert curator.load_state().get("last_run_summary_shown_at") is None
    assert notices.recent_run_notice_text(mark_shown=True)
    assert notices.recent_run_notice_text(mark_shown=True) is None
