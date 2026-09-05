"""curator.cli — the ``curator <subcommand>`` surface (ports of the hermes_cli curator tests).

Message parity note: Hermes prints ``hermes curator <verb>``; the plugin's
program name is ``curator``, so the same hints read ``curator <verb>``.
"""

from __future__ import annotations

import argparse
import io
import json
from argparse import Namespace
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from conftest import write_skill


def _ns(**kwargs):
    return SimpleNamespace(**kwargs)


def _run(fn, args) -> tuple:
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = fn(args)
    return rc, buf.getvalue()


# --- argparse wiring ---------------------------------------------------------

def test_all_subcommands_registered(home):
    from curator import cli
    parser = argparse.ArgumentParser(prog="curator")
    cli.register_cli(parser)
    names = {n for n, *_ in cli._SUBCOMMANDS}
    assert names == {"status", "usage", "run", "pause", "resume", "pin", "unpin", "list-unmanaged", "adopt", "restore",
                     "list-archived", "archive", "prune", "backup", "rollback", "ledger", "purge"}
    args = parser.parse_args(["archive", "my-skill"])
    assert args.skill == "my-skill" and args.func.__name__ == "_cmd_archive"
    args = parser.parse_args(["prune", "--days", "45", "--yes", "--dry-run"])
    assert args.days == 45 and args.yes is True and args.dry_run is True and args.func.__name__ == "_cmd_prune"
    args = parser.parse_args(["adopt", "--all-unmanaged", "--dry-run"])
    assert args.func is cli._cmd_adopt and args.all_unmanaged is True and args.dry_run is True and args.skill == []
    named = parser.parse_args(["adopt", "alpha", "beta"])
    assert named.skill == ["alpha", "beta"] and named.all_unmanaged is False
    args = parser.parse_args(["usage", "--sort", "recent", "--provenance", "hub", "--json"])
    assert args.func is cli._cmd_usage and args.sort == "recent" and args.provenance == "hub" and args.json is True
    args = parser.parse_args(["run", "--dry-run", "--consolidate", "--background"])
    assert args.dry_run and args.consolidate and args.background
    args = parser.parse_args(["rollback", "abc123", "-y"])
    assert args.entry_id == "abc123" and args.yes
    args = parser.parse_args(["rollback", "--list"])
    assert args.list is True and args.entry_id is None
    args = parser.parse_args(["ledger", "--skill", "x", "--limit", "5"])
    assert args.skill == "x" and args.limit == 5
    args = parser.parse_args(["purge", "--days", "30", "--dry-run"])
    assert args.days == 30 and args.dry_run
    args = parser.parse_args(["backup", "--reason", "before-refactor"])
    assert args.reason == "before-refactor"


def test_cli_main_no_args_prints_help(home, capsys):
    from curator import cli
    assert cli.cli_main([]) == 0
    assert "usage" in capsys.readouterr().out.lower()


# --- archive / prune ---------------------------------------------------------

def test_archive_refuses_pinned(home, monkeypatch, capsys):
    from curator import cli, skill_usage
    monkeypatch.setattr(skill_usage, "get_record", lambda name: {"pinned": True})
    called = []
    monkeypatch.setattr(skill_usage, "archive_skill", lambda name: called.append(name) or (True, "should not get here"))
    assert cli._cmd_archive(_ns(skill="pinned-skill")) == 1
    assert called == []
    out = capsys.readouterr().out
    assert "pinned" in out.lower() and "curator unpin pinned-skill" in out


def test_archive_runs_as_user_actor(home):
    from curator import cli, skill_ledger
    write_skill(home / "skills", "mine")
    rc, out = _run(cli._cmd_archive, _ns(skill="mine"))
    assert rc == 0 and "archived to" in out
    entry = [r for r in skill_ledger.list_entries("mine") if r["action"] == "archive"][0]
    assert entry["actor"] == "user"
    rc, out = _run(cli._cmd_restore, _ns(skill="mine"))
    assert rc == 0 and "restored to" in out
    assert cli._cmd_archive(_ns(skill="absent")) == 1


def _mk_record(name, *, idle_days=0, pinned=False, state="active", created_idle_days=None):
    now = datetime.now(timezone.utc)
    last_activity = (now - timedelta(days=idle_days)).isoformat() if idle_days else None
    created_delta = created_idle_days if created_idle_days is not None else idle_days
    return {"name": name, "state": state, "pinned": pinned, "last_activity_at": last_activity,
            "created_at": (now - timedelta(days=created_delta)).isoformat(),
            "activity_count": 0 if idle_days == 0 and last_activity is None else 1}


def test_prune_filters_and_thresholds(home, monkeypatch, capsys):
    from curator import cli, skill_usage
    rows = [_mk_record("old", idle_days=120), _mk_record("pinned-old", idle_days=120, pinned=True),
            _mk_record("archived-old", idle_days=120, state="archived"), _mk_record("young", idle_days=10),
            _mk_record("never-used-old", idle_days=0, created_idle_days=200)]
    monkeypatch.setattr(skill_usage, "curated_report", lambda: rows)
    archived = []
    monkeypatch.setattr(skill_usage, "archive_skill", lambda n: archived.append(n) or (True, "ok"))
    assert cli._cmd_prune(_ns(days=90, yes=True, dry_run=False)) == 0
    assert archived == ["never-used-old", "old"]  # sorted by idle days desc
    out = capsys.readouterr().out
    assert "2 skill(s) idle >= 90d" in out and "archived 2/2" in out


def test_prune_dry_run_and_validation_and_nothing(home, monkeypatch, capsys):
    from curator import cli, skill_usage
    monkeypatch.setattr(skill_usage, "curated_report", lambda: [_mk_record("old", idle_days=120)])
    archived = []
    monkeypatch.setattr(skill_usage, "archive_skill", lambda n: archived.append(n) or (True, "ok"))
    assert cli._cmd_prune(_ns(days=90, yes=False, dry_run=True)) == 0
    assert archived == [] and "dry run" in capsys.readouterr().out
    assert cli._cmd_prune(_ns(days=0, yes=True, dry_run=False)) == 2
    assert "--days must be >= 1" in capsys.readouterr().err
    monkeypatch.setattr(skill_usage, "curated_report", lambda: [_mk_record("young", idle_days=1)])
    assert cli._cmd_prune(_ns(days=90, yes=True, dry_run=False)) == 0
    assert "nothing to prune" in capsys.readouterr().out


def test_prune_confirmation_abort_and_failure_reporting(home, monkeypatch, capsys):
    from curator import cli, skill_usage
    monkeypatch.setattr(skill_usage, "curated_report", lambda: [_mk_record("old", idle_days=120)])
    monkeypatch.setattr("builtins.input", lambda prompt: "n")
    assert cli._cmd_prune(_ns(days=90, yes=False, dry_run=False)) == 1
    assert "aborted" in capsys.readouterr().out
    monkeypatch.setattr(skill_usage, "archive_skill", lambda n: (False, "nope"))
    assert cli._cmd_prune(_ns(days=90, yes=True, dry_run=False)) == 1
    assert "failures:" in capsys.readouterr().out


# --- pin / unpin -------------------------------------------------------------

def _stub_pin(monkeypatch, *, managed: bool):
    from curator import skill_usage
    calls = []
    monkeypatch.setattr(skill_usage, "is_agent_created", lambda name: True)
    monkeypatch.setattr(skill_usage, "is_curator_managed", lambda name: managed)
    monkeypatch.setattr(skill_usage, "set_pinned", lambda name, pinned: (calls.append((name, pinned)), True)[1])
    return calls


def test_pin_unmanaged_records_flag_and_prints_adopt_hint(home, monkeypatch, capsys):
    from curator import cli
    calls = _stub_pin(monkeypatch, managed=False)
    assert cli._cmd_pin(_ns(skill="legacy-skill")) == 0
    assert calls == [("legacy-skill", True)]
    out = capsys.readouterr().out
    assert "unmanaged" in out and "curator adopt legacy-skill" in out and "will bypass auto-transitions" not in out


def test_pin_managed_keeps_bypass_message(home, monkeypatch, capsys):
    from curator import cli
    calls = _stub_pin(monkeypatch, managed=True)
    assert cli._cmd_pin(_ns(skill="agent-skill")) == 0
    assert calls == [("agent-skill", True)]
    out = capsys.readouterr().out
    assert "will bypass auto-transitions" in out and "unmanaged" not in out


def test_unpin_unmanaged_says_it_was_never_managed(home, monkeypatch, capsys):
    from curator import cli
    calls = _stub_pin(monkeypatch, managed=False)
    assert cli._cmd_unpin(_ns(skill="legacy-skill")) == 0
    assert calls == [("legacy-skill", False)]
    out = capsys.readouterr().out
    assert "unmanaged" in out and "never under auto-transitions" in out


def test_pin_still_refuses_bundled_skills(home, monkeypatch, capsys):
    from curator import cli, skill_usage
    calls = _stub_pin(monkeypatch, managed=True)
    monkeypatch.setattr(skill_usage, "is_agent_created", lambda name: False)
    assert cli._cmd_pin(_ns(skill="bundled-skill")) == 1
    assert calls == [] and "cannot pin" in capsys.readouterr().out


def test_cli_pin_refuses_bundled_skill_end_to_end(home, capsys):
    from curator import cli
    skills = home / "skills"
    write_skill(skills, "ship-skill")
    (skills / ".bundled_manifest").write_text("ship-skill:abc\n", encoding="utf-8")
    assert cli._cmd_pin(_ns(skill="ship-skill")) == 1
    assert "bundled" in capsys.readouterr().out.lower()


def test_pin_fails_loudly_when_write_does_not_land(home, monkeypatch):
    from curator import cli, skill_usage
    name = "sentinel-protected-skill"
    monkeypatch.setattr(skill_usage, "PROTECTED_BUILTIN_SKILLS", {name})
    write_skill(home / "skills", name)
    assert skill_usage.is_agent_created(name) is True and skill_usage.is_curation_eligible(name) is False
    rc, out = _run(cli._cmd_pin, _ns(skill=name))
    assert rc != 0 and "pin" in out.lower()
    assert not skill_usage.get_record(name).get("pinned")


def test_pinned_eligible_unmanaged_skill_visible_in_status(home):
    from curator import cli, skill_usage
    write_skill(home / "skills", "legacy-skill")
    rc, out = _run(cli._cmd_pin, _ns(skill="legacy-skill"))
    assert rc == 0 and skill_usage.get_record("legacy-skill").get("pinned") is True
    rc, status = _run(cli._cmd_status, Namespace())
    assert rc == 0 and "legacy-skill" in status and "pinned" in status.lower()


def test_pin_managed_skill_end_to_end(home):
    from curator import cli, skill_usage
    write_skill(home / "skills", "managed-skill")
    skill_usage.mark_agent_created("managed-skill")
    rc, out = _run(cli._cmd_pin, _ns(skill="managed-skill"))
    assert rc == 0 and "pinned" in out.lower()
    assert skill_usage.get_record("managed-skill").get("pinned") is True
    rc, status = _run(cli._cmd_status, Namespace())
    assert rc == 0 and "managed-skill" in status and "pinned (1): managed-skill" in status


# --- status / list-unmanaged / adopt ----------------------------------------

def test_status_config_block_and_empty(home):
    from curator import cli
    rc, out = _run(cli._cmd_status, Namespace())
    assert rc == 0
    assert "curator: ENABLED" in out and "runs:           0" in out and "last run:       never" in out
    assert "interval:       every 7d" in out and "stale after:    30d unused" in out
    assert "consolidate:    off (prune-only; LLM merge pass opt-in)" in out
    assert "no curator-managed skills" in out


def test_status_shows_paused_disabled_and_multiline_summary(home, set_config):
    from curator import cli, curator
    curator.save_state({**curator.load_state(), "last_run_summary": "auto: x\n  • a → b", "last_report_path": "/nope",
                        "run_count": 3, "last_run_at": datetime.now(timezone.utc).isoformat()})
    curator.set_paused(True)
    rc, out = _run(cli._cmd_status, Namespace())
    assert "curator: PAUSED" in out and "runs:           3" in out
    assert "last summary:   auto: x\n                    • a → b" in out
    assert "last report:    /nope (missing)" in out
    curator.set_paused(False)
    set_config({"curator": {"enabled": False, "interval_hours": 36}})
    rc, out = _run(cli._cmd_status, Namespace())
    assert "curator: DISABLED" in out and "interval:       every 36h" in out


def test_status_rankings_and_unmanaged_summary(home):
    from curator import cli, skill_usage
    skills = home / "skills"
    for name in ("busy", "quiet", "legacy-one"):
        write_skill(skills, name)
    skill_usage.mark_agent_created("busy")
    skill_usage.mark_agent_created("quiet")
    for _ in range(3):
        skill_usage.bump_view("busy")
    rc, out = _run(cli._cmd_status, Namespace())
    assert "curator-managed skills: 2 total  (agent-created=2  bundled=0)" in out
    assert "active     2" in out
    assert "least recently active (top 5)" in out and "most active (top 5)" in out and "least active (top 5)" in out
    assert "unmanaged (no provenance marker): 1 total" in out
    assert "pre-dates marker    1" in out and "curator adopt <name>" in out


def test_list_unmanaged_itemizes_and_explains(home):
    from curator import cli, skill_usage
    skills = home / "skills"
    write_skill(skills, "legacy-one")
    write_skill(skills, "managed-one")
    skill_usage.mark_agent_created("managed-one")
    rc, out = _run(cli._cmd_list_unmanaged, Namespace())
    assert rc == 0 and "legacy-one" in out and "managed-one" not in out
    assert ("no marker" in out or "created_by:null" in out) and "curator adopt" in out
    skill_usage.mark_agent_created("legacy-one")
    rc, out = _run(cli._cmd_list_unmanaged, Namespace())
    assert "no unmanaged skills" in out


def test_adopt_paths(home, monkeypatch):
    from curator import cli, skill_usage
    skills = home / "skills"
    write_skill(skills, "a")
    write_skill(skills, "b")
    assert _run(cli._cmd_adopt, _ns(skill=[], all_unmanaged=False, dry_run=False, yes=False))[0] == 1
    assert _run(cli._cmd_adopt, _ns(skill=["a"], all_unmanaged=True, dry_run=False, yes=False))[0] == 1
    rc, out = _run(cli._cmd_adopt, _ns(skill=[], all_unmanaged=True, dry_run=True, yes=False))
    assert rc == 0 and "would adopt 2 skill(s)" in out and not skill_usage.is_curator_managed("a")
    monkeypatch.setattr("builtins.input", lambda prompt: "n")
    rc, out = _run(cli._cmd_adopt, _ns(skill=[], all_unmanaged=True, dry_run=False, yes=False))
    assert rc == 1 and "aborted" in out
    rc, out = _run(cli._cmd_adopt, _ns(skill=[], all_unmanaged=True, dry_run=False, yes=True))
    assert rc == 0 and "adopted 2/2" in out
    assert skill_usage.is_curator_managed("a") and skill_usage.is_curator_managed("b")
    rc, out = _run(cli._cmd_adopt, _ns(skill=[], all_unmanaged=True, dry_run=False, yes=True))
    assert rc == 0 and "no unmanaged skills to adopt" in out
    rc, out = _run(cli._cmd_adopt, _ns(skill=["missing"], all_unmanaged=False, dry_run=False, yes=False))
    assert rc == 1 and "not found" in out


# --- run ---------------------------------------------------------------------

def _run_args(**kwargs):
    values = {"dry_run": False, "synchronous": False, "background": False, "consolidate": False}
    values.update(kwargs)
    return SimpleNamespace(**values)


def test_run_defaults_to_synchronous(home, monkeypatch, capsys):
    from curator import cli, curator
    calls = []
    monkeypatch.setattr(curator, "is_enabled", lambda: True)
    monkeypatch.setattr(curator, "run_curator_review", lambda **kwargs: calls.append(kwargs) or {"auto_transitions": {}})
    assert cli._cmd_run(_run_args()) == 0
    assert calls[0]["synchronous"] is True and calls[0]["dry_run"] is False and calls[0]["consolidate"] is None
    out = capsys.readouterr().out
    assert "background" not in out and "consolidation is off" in out


def test_run_background_consolidate_and_disabled(home, monkeypatch, capsys):
    from curator import cli, curator
    calls = []
    monkeypatch.setattr(curator, "run_curator_review", lambda **kwargs: calls.append(kwargs) or {
        "auto_transitions": {"checked": 2, "marked_stale": 1, "archived": 0, "reactivated": 0}})
    assert cli._cmd_run(_run_args(background=True, consolidate=True)) == 0
    assert calls[0]["synchronous"] is False and calls[0]["consolidate"] is True
    out = capsys.readouterr().out
    assert "llm pass running in background" in out and "auto: checked=2 stale=1 archived=0 reactivated=0" in out
    assert "consolidation is off" not in out
    monkeypatch.setattr(curator, "is_enabled", lambda: False)
    assert cli._cmd_run(_run_args()) == 1
    assert "disabled via config" in capsys.readouterr().out


def test_dry_run_default_reports_synchronous_wording(home, monkeypatch, capsys):
    from curator import cli, curator
    monkeypatch.setattr(curator, "is_enabled", lambda: True)
    monkeypatch.setattr(curator, "run_curator_review", lambda **kwargs: {"auto_transitions": {"checked": 4}})
    assert cli._cmd_run(_run_args(dry_run=True)) == 0
    out = capsys.readouterr().out
    assert "When the report lands" not in out and "Read the report with `curator status`" in out
    assert "auto (preview): 4 candidate skill(s)" in out


def test_pause_resume(home, capsys):
    from curator import cli, curator
    assert cli._cmd_pause(Namespace()) == 0 and curator.is_paused()
    assert cli._cmd_resume(Namespace()) == 0 and not curator.is_paused()
    assert "curator: paused" in capsys.readouterr().out


# --- usage -------------------------------------------------------------------

def _fake_rows():
    return [{"name": "agent-skill", "provenance": "agent", "state": "active", "use_count": 2, "view_count": 1, "patch_count": 0,
             "activity_count": 3, "last_activity_at": "2026-05-01T10:00:00+00:00", "created_at": "2026-01-01T00:00:00+00:00", "_persisted": True},
            {"name": "bundled-skill", "provenance": "bundled", "state": "active", "use_count": 9, "view_count": 4, "patch_count": 0,
             "activity_count": 13, "last_activity_at": "2026-05-10T10:00:00+00:00", "created_at": "2026-01-01T00:00:00+00:00", "_persisted": True},
            {"name": "hub-skill", "provenance": "hub", "state": "active", "use_count": 0, "view_count": 0, "patch_count": 0,
             "activity_count": 0, "last_activity_at": None, "created_at": "2026-01-01T00:00:00+00:00", "_persisted": False}]


def test_usage_lists_all_provenances_and_sorts(home, monkeypatch, capsys):
    from curator import cli, skill_usage
    monkeypatch.setattr(skill_usage, "usage_report", _fake_rows)
    assert cli._cmd_usage(_ns(sort="activity", provenance=None, json=False)) == 0
    out = capsys.readouterr().out
    assert "agent=1" in out and "bundled=1" in out and "hub=1" in out
    assert out.index("bundled-skill") < out.index("agent-skill") < out.index("hub-skill")
    assert cli._cmd_usage(_ns(sort="name", provenance=None, json=False)) == 0
    out = capsys.readouterr().out
    assert out.index("agent-skill") < out.index("bundled-skill") < out.index("hub-skill")
    assert cli._cmd_usage(_ns(sort="recent", provenance="hub", json=True)) == 0
    rows = json.loads(capsys.readouterr().out)
    assert [r["name"] for r in rows] == ["hub-skill"]


def test_usage_empty(home, monkeypatch, capsys):
    from curator import cli, skill_usage
    monkeypatch.setattr(skill_usage, "usage_report", lambda: [])
    assert cli._cmd_usage(_ns(sort="activity", provenance=None, json=False)) == 0
    assert "no skills found" in capsys.readouterr().out


# --- list-archived / restore / backup / rollback -----------------------------

def test_list_archived_and_restore(home, capsys):
    from curator import cli, skill_usage
    assert cli._cmd_list_archived(Namespace()) == 0 and "no archived skills" in capsys.readouterr().out
    write_skill(home / "skills", "x")
    skill_usage.archive_skill("x")
    assert cli._cmd_list_archived(Namespace()) == 0 and capsys.readouterr().out.strip() == "x"
    assert cli._cmd_restore(_ns(skill="x")) == 0 and "restored" in capsys.readouterr().out
    assert cli._cmd_restore(_ns(skill="x")) == 1


def test_backup_command(home, capsys, set_config):
    from curator import cli, curator_backup
    write_skill(home / "skills", "x")
    assert cli._cmd_backup(_ns(reason="before-refactor")) == 0
    assert "snapshot created at" in capsys.readouterr().out
    assert curator_backup.list_backups()[0]["reason"] == "before-refactor"
    set_config({"curator": {"backup": {"enabled": False}}})
    assert cli._cmd_backup(_ns(reason=None)) == 1
    assert "backups are disabled" in capsys.readouterr().out


def test_rollback_whole_tree_paths(home, monkeypatch, capsys):
    from curator import cli, curator_backup
    assert cli._cmd_rollback(_ns(entry_id=None, list=False, backup_id=None, yes=True)) == 1
    assert "no snapshots exist yet" in capsys.readouterr().out
    write_skill(home / "skills", "x")
    snap = curator_backup.snapshot_skills(reason="r1")
    assert cli._cmd_rollback(_ns(entry_id=None, list=True, backup_id=None, yes=False)) == 0
    assert snap.name in capsys.readouterr().out
    assert cli._cmd_rollback(_ns(entry_id=None, list=False, backup_id="bogus", yes=True)) == 1
    assert "no snapshot matching id 'bogus'" in capsys.readouterr().out
    monkeypatch.setattr("builtins.input", lambda prompt: "n")
    assert cli._cmd_rollback(_ns(entry_id=None, list=False, backup_id=snap.name, yes=False)) == 1
    assert cli._cmd_rollback(_ns(entry_id=None, list=False, backup_id=snap.name, yes=True)) == 0
    out = capsys.readouterr().out
    assert "Rollback target:" in out and "restored from snapshot" in out


def test_rollback_ledger_entry_paths(home, capsys):
    from curator import cli, skill_ledger
    assert cli._cmd_rollback(_ns(entry_id="nope", list=False, backup_id=None, yes=True)) == 1
    assert "no ledger entry 'nope'" in capsys.readouterr().out
    write_skill(home / "skills", "x")
    skill_md = home / "skills" / "x" / "SKILL.md"
    before = skill_ledger.snapshot_paths(skill_md)
    skill_md.write_text("changed", encoding="utf-8")
    entry_id = skill_ledger.append_entry("patch", "x", before=before, after=skill_ledger.snapshot_paths(skill_md))
    assert cli._cmd_rollback(_ns(entry_id=entry_id, list=False, backup_id=None, yes=True)) == 0
    out = capsys.readouterr().out
    assert "Rollback target: ledger entry" in out and "rolled back entry" in out
    assert "changed" not in skill_md.read_text(encoding="utf-8")


# --- ledger / purge ----------------------------------------------------------

def test_ledger_listing(home, capsys):
    from curator import cli, skill_ledger
    assert cli._cmd_ledger(_ns(skill=None, limit=20)) == 0
    assert "ledger is empty" in capsys.readouterr().out
    skill_ledger.append_entry("delete", "a", evidence={"absorbed_into": "umbrella"})
    skill_ledger.append_entry("rollback", "a", evidence={"rollback_target": "abc"})
    assert cli._cmd_ledger(_ns(skill="a", limit=20)) == 0
    out = capsys.readouterr().out
    assert "absorbed into 'umbrella'" in out and "rollback of abc" in out and "curator rollback <id>" in out


def test_purge_paths(home, monkeypatch, capsys, set_config):
    from curator import cli, skill_ledger
    import os, time
    assert cli._cmd_purge(_ns(days=None, dry_run=False, yes=True)) == 1
    assert "purge disabled" in capsys.readouterr().out
    assert cli._cmd_purge(_ns(days=30, dry_run=False, yes=True)) == 0
    assert "no archive directory" in capsys.readouterr().out
    archive = home / "skills" / ".archive"
    old = write_skill(archive, "old")
    write_skill(archive, "new")
    ancient = time.time() - 400 * 86400
    os.utime(old, (ancient, ancient))
    set_config({"curator": {"archive_ttl_days": 180}})
    assert cli._cmd_purge(_ns(days=None, dry_run=True, yes=False)) == 0
    out = capsys.readouterr().out
    assert "old" in out and "dry run" in out and old.exists()
    assert cli._cmd_purge(_ns(days=None, dry_run=False, yes=True)) == 0
    assert "purged 1 archived skill(s)" in capsys.readouterr().out
    assert not old.exists() and (archive / "new").exists()
    entry = skill_ledger.list_entries(skill="old")[0]
    assert entry["action"] == "purge" and entry["actor"] == "user" and entry["evidence"]["ttl_days"] == 180
    assert entry["before"]
    assert cli._cmd_purge(_ns(days=180, dry_run=False, yes=True)) == 0
    assert "no archived skills older than 180d" in capsys.readouterr().out


# --- helpers -----------------------------------------------------------------

def test_fmt_ts_and_idle_days(home):
    from curator import cli
    now = datetime.now(timezone.utc)
    assert cli._fmt_ts(None) == "never"
    assert cli._fmt_ts("garbage") == "garbage"
    assert cli._fmt_ts((now - timedelta(seconds=5)).isoformat()).endswith("s ago")
    assert cli._fmt_ts((now - timedelta(minutes=5)).isoformat()) == "5m ago"
    assert cli._fmt_ts((now - timedelta(hours=3)).isoformat()) == "3h ago"
    assert cli._fmt_ts((now - timedelta(days=2)).isoformat()) == "2d ago"
    assert cli._idle_days({"last_activity_at": (now - timedelta(days=4)).isoformat()}) == 4
    assert cli._idle_days({"created_at": (now - timedelta(days=9)).isoformat()}) == 9
    assert cli._idle_days({}) is None
