"""curator.skill_usage — sidecar telemetry + provenance filtering.

Telemetry, provenance, archive/restore, reporting and eligibility.
"""

from __future__ import annotations

import json
import multiprocessing as mp
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from conftest import write_skill


def _bump_view_many(home: str, skill_name: str, iterations: int) -> None:
    os.environ["CURATOR_HOME"] = home
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from curator.skill_usage import bump_view
    for _ in range(iterations):
        bump_view(skill_name)


# ---------------------------------------------------------------------------
# Round-trip
# ---------------------------------------------------------------------------

def test_empty_usage_returns_empty_dict(home):
    from curator.skill_usage import load_usage
    assert load_usage() == {}


def test_save_and_load_roundtrip(home):
    from curator.skill_usage import load_usage, save_usage
    assert save_usage({"skill-a": {"use_count": 3, "state": "active"}}) is True
    loaded = load_usage()
    assert loaded["skill-a"]["use_count"] == 3
    assert loaded["skill-a"]["state"] == "active"


def test_get_record_missing_returns_empty_record(home):
    from curator.skill_usage import get_record
    rec = get_record("nonexistent")
    assert rec["use_count"] == 0
    assert rec["view_count"] == 0
    assert rec["state"] == "active"
    assert rec["pinned"] is False
    assert rec["archived_at"] is None
    assert rec["created_by"] is None
    assert rec["patch_generation"] == 0


def test_load_usage_handles_corrupt_file(home):
    from curator.skill_usage import load_usage, _usage_file
    _usage_file().write_text("{ not json }", encoding="utf-8")
    assert load_usage() == {}


def test_load_usage_drops_non_dict_values(home):
    from curator.skill_usage import load_usage, _usage_file
    _usage_file().write_text(json.dumps({"ok": {"use_count": 1}, "junk": 5}), encoding="utf-8")
    assert load_usage() == {"ok": {"use_count": 1}}


# ---------------------------------------------------------------------------
# Counter bumps
# ---------------------------------------------------------------------------

def test_bump_view_increments_and_timestamps(home):
    from curator.skill_usage import bump_view, get_record
    bump_view("my-skill")
    bump_view("my-skill")
    rec = get_record("my-skill")
    assert rec["view_count"] == 2
    assert rec["last_viewed_at"] is not None


def test_skill_reuse_and_post_patch_reuse_are_derived_atomically(home, monkeypatch):
    from curator import lifecycle
    from curator.skill_usage import bump_patch, bump_use, get_record, record_created

    events = []
    monkeypatch.setattr(lifecycle, "has_hook", lambda name: True)
    monkeypatch.setattr(lifecycle, "invoke_hook", lambda name, **kwargs: events.append((name, kwargs)))

    record_created("private-skill-name", agent_created=True, task_id="task")
    bump_use("private-skill-name", task_id="task")
    bump_use("private-skill-name", task_id="task")
    bump_patch("private-skill-name", task_id="task")
    bump_use("private-skill-name", task_id="task")
    bump_use("private-skill-name", task_id="task")

    loaded = [event for _, event in events if event["action"] == "loaded"]
    assert [e["reused"] for e in loaded] == [False, True, True, True]
    assert [e["reuse_after_patch"] for e in loaded] == [False, False, True, False]
    assert all(e["provenance"] == "agent_created" for e in loaded)
    record = get_record("private-skill-name")
    assert record["use_count"] == 4
    assert record["patch_generation"] == 1
    assert record["last_reused_patch_generation"] == 1


def test_skill_state_events_emit_only_for_real_transitions(home, monkeypatch):
    from curator import lifecycle
    from curator.skill_usage import STATE_ACTIVE, STATE_ARCHIVED, STATE_STALE, record_created, set_state

    events = []
    monkeypatch.setattr(lifecycle, "has_hook", lambda name: True)
    monkeypatch.setattr(lifecycle, "invoke_hook", lambda name, **kwargs: events.append(kwargs))

    record_created("my-skill", agent_created=True)
    write_skill(home / "skills", "my-skill")
    for state in (STATE_STALE, STATE_STALE, STATE_ARCHIVED, STATE_ARCHIVED, STATE_ACTIVE, STATE_ACTIVE):
        set_state("my-skill", state)
    assert [e["action"] for e in events] == ["created", "stale", "archived", "restored"]


def test_skill_event_is_not_emitted_when_usage_state_cannot_commit(home, monkeypatch):
    from curator import lifecycle, skill_usage
    events = []
    monkeypatch.setattr(lifecycle, "has_hook", lambda name: True)
    monkeypatch.setattr(lifecycle, "invoke_hook", lambda name, **kwargs: events.append(kwargs))
    monkeypatch.setattr(skill_usage, "save_usage", lambda data: False)
    skill_usage.bump_use("private-skill-name")
    assert events == []


def test_created_skill_does_not_inherit_stale_identity_or_continuity(home, monkeypatch):
    from curator import lifecycle, skill_usage
    events = []
    monkeypatch.setattr(lifecycle, "has_hook", lambda name: True)
    monkeypatch.setattr(lifecycle, "invoke_hook", lambda name, **kwargs: events.append(kwargs))
    skill_usage.save_usage({"recreated": {
        "created_by": "agent", "use_count": 11, "patch_count": 4, "patch_generation": 4,
        "last_reused_patch_generation": 3, "pinned": True, "state": skill_usage.STATE_ARCHIVED}})
    skill_usage.record_created("recreated", agent_created=False)
    skill_usage.bump_use("recreated")
    record = skill_usage.get_record("recreated")
    assert record["created_by"] is None
    assert record["use_count"] == 1
    assert record["patch_count"] == 0
    assert record["patch_generation"] == 0
    assert record["last_reused_patch_generation"] == 0
    assert record["pinned"] is False
    assert record["state"] == skill_usage.STATE_ACTIVE
    assert [e["provenance"] for e in events] == ["local", "local"]
    assert events[-1]["reused"] is False
    assert events[-1]["reuse_after_patch"] is False


def test_malformed_usage_counters_recover_without_losing_patch_reuse(home, monkeypatch):
    from curator import lifecycle, skill_usage
    events = []
    monkeypatch.setattr(lifecycle, "has_hook", lambda name: True)
    monkeypatch.setattr(lifecycle, "invoke_hook", lambda name, **kwargs: events.append(kwargs))
    skill_usage.save_usage({"damaged": {
        "view_count": "not-a-number", "use_count": "not-a-number",
        "patch_generation": 1, "last_reused_patch_generation": 999}})
    skill_usage.bump_view("damaged")
    skill_usage.bump_use("damaged")
    skill_usage.bump_patch("damaged")
    skill_usage.bump_use("damaged")
    record = skill_usage.get_record("damaged")
    assert record["view_count"] == 1
    assert record["use_count"] == 2
    assert record["patch_generation"] == 2
    assert record["last_reused_patch_generation"] == 2
    loaded = [e for e in events if e["action"] == "loaded"]
    assert [e["reused"] for e in loaded] == [False, True]
    assert [e["reuse_after_patch"] for e in loaded] == [False, True]


def test_bumps_do_not_corrupt_other_skills(home):
    from curator.skill_usage import bump_use, bump_view, get_record
    bump_view("skill-a")
    bump_use("skill-b")
    bump_view("skill-a")
    assert get_record("skill-a")["view_count"] == 2
    assert get_record("skill-a")["use_count"] == 0
    assert get_record("skill-b")["use_count"] == 1


def test_concurrent_bump_view_preserves_all_updates(home):
    from curator.skill_usage import get_record
    process_count, iterations = 6, 25
    ctx = mp.get_context("spawn")
    procs = [ctx.Process(target=_bump_view_many, args=(str(home), "shared-skill", iterations))
             for _ in range(process_count)]
    for p in procs:
        p.start()
    for p in procs:
        p.join(timeout=30)
    for p in procs:
        assert p.exitcode == 0
    assert get_record("shared-skill")["view_count"] == process_count * iterations


# ---------------------------------------------------------------------------
# State transitions
# ---------------------------------------------------------------------------

def test_set_state_active(home):
    from curator.skill_usage import STATE_ACTIVE, get_record, set_state
    set_state("x", STATE_ACTIVE)
    assert get_record("x")["state"] == "active"


def test_restoring_from_archive_clears_timestamp(home):
    from curator.skill_usage import STATE_ACTIVE, STATE_ARCHIVED, get_record, set_state
    set_state("x", STATE_ARCHIVED)
    assert get_record("x")["archived_at"] is not None
    set_state("x", STATE_ACTIVE)
    assert get_record("x")["archived_at"] is None


def test_set_state_invalid_is_noop(home):
    from curator.skill_usage import get_record, set_state
    set_state("x", "bogus")
    assert get_record("x")["state"] == "active"


def test_forget_removes_record(home):
    from curator.skill_usage import bump_view, forget, load_usage
    bump_view("x")
    assert "x" in load_usage()
    forget("x")
    assert "x" not in load_usage()


def test_latest_activity_at_and_activity_count(home):
    from curator.skill_usage import activity_count, latest_activity_at
    rec = {"last_used_at": "2026-01-01T00:00:00+00:00", "last_viewed_at": "2026-02-01T00:00:00+00:00",
           "last_patched_at": None, "created_at": "2026-03-01T00:00:00+00:00",
           "use_count": 1, "view_count": "2", "patch_count": None}
    assert latest_activity_at(rec) == "2026-02-01T00:00:00+00:00"  # created_at excluded
    assert activity_count(rec) == 3
    assert latest_activity_at({"created_at": "2026-03-01T00:00:00+00:00"}) is None


# ---------------------------------------------------------------------------
# Provenance filter — the load-bearing safety check
# ---------------------------------------------------------------------------

def test_agent_created_excludes_unadopted_skills(home):
    from curator.skill_usage import list_agent_created_skill_names, mark_agent_created
    skills_dir = home / "skills"
    write_skill(skills_dir, "hand-written", category="github")
    write_skill(skills_dir, "my-skill")
    mark_agent_created("my-skill")
    names = list_agent_created_skill_names()
    assert "my-skill" in names
    assert "hand-written" not in names


def test_is_agent_created(home, tmp_path, set_config):
    from curator.skill_usage import is_agent_created
    write_skill(tmp_path / "ext", "ext-only")
    set_config({"skills": {"external_dirs": [str(tmp_path / "ext")]}})
    assert is_agent_created("my-skill") is True
    assert is_agent_created("ext-only") is False


def test_external_dir_skill_is_never_eligible(home, set_config, tmp_path):
    from curator import skill_usage as u
    ext = tmp_path / "ext"
    write_skill(ext, "shared")
    set_config({"skills": {"external_dirs": [str(ext)]}})
    assert u.is_curation_eligible("shared") is False
    assert u.is_agent_created("shared") is False
    ok, msg = u.archive_skill("shared")
    assert ok is False and "external" in msg
    ok, msg = u.adopt_skill("shared")
    assert ok is False and "external" in msg


def test_seed_record_if_missing_only_for_eligible_and_only_once(home, tmp_path, set_config):
    from curator import skill_usage as u
    write_skill(home / "skills", "fresh")
    u.seed_record_if_missing("fresh")
    first = u.load_usage()["fresh"]
    u.seed_record_if_missing("fresh")
    assert u.load_usage()["fresh"] == first
    write_skill(tmp_path / "ext", "shared")
    set_config({"skills": {"external_dirs": [str(tmp_path / "ext")]}})
    u.seed_record_if_missing("shared")  # external -> not eligible
    assert "shared" not in u.load_usage()


# ---------------------------------------------------------------------------
# Archive / restore
# ---------------------------------------------------------------------------

def test_archive_and_restore_roundtrip(home):
    from curator import skill_usage as u
    skills_dir = home / "skills"
    write_skill(skills_dir, "mine")
    ok, msg = u.archive_skill("mine")
    assert ok, msg
    assert not (skills_dir / "mine").exists()
    assert (skills_dir / ".archive" / "mine" / "SKILL.md").exists()
    assert u.get_record("mine")["state"] == u.STATE_ARCHIVED
    assert u.get_record("mine")["archived_at"] is not None
    assert u.list_archived_skill_names() == ["mine"]
    ok, msg = u.restore_skill("mine")
    assert ok, msg
    assert (skills_dir / "mine" / "SKILL.md").exists()
    assert u.get_record("mine")["state"] == u.STATE_ACTIVE
    assert u.list_archived_skill_names() == []


def test_archive_flattens_nested_skill_and_suffixes_collision(home):
    from curator import skill_usage as u
    skills_dir = home / "skills"
    write_skill(skills_dir, "dup", category="cat")
    assert u.archive_skill("dup")[0]
    assert (skills_dir / ".archive" / "dup").is_dir()
    write_skill(skills_dir, "dup")
    ok, msg = u.archive_skill("dup")
    assert ok, msg
    suffixed = [p.name for p in (skills_dir / ".archive").iterdir() if p.name.startswith("dup-")]
    assert len(suffixed) == 1 and len(suffixed[0]) == len("dup-") + 14


def test_restore_picks_exact_name_then_newest_timestamped_never_prefix_sibling(home):
    from curator import skill_usage as u
    skills_dir = home / "skills"
    archive = skills_dir / ".archive"
    write_skill(archive, "git-helpers")
    write_skill(archive, "git-20260101000000")
    write_skill(archive, "git-20260201000000")
    ok, _ = u.restore_skill("git")
    assert ok
    assert (skills_dir / "git" / "SKILL.md").exists()
    assert not (archive / "git-20260201000000").exists()
    assert (archive / "git-20260101000000").exists()
    assert (archive / "git-helpers").exists()


def test_restore_refuses_when_destination_exists_or_missing_archive(home):
    from curator import skill_usage as u
    skills_dir = home / "skills"
    assert u.restore_skill("nothing") == (False, "no archive directory")
    write_skill(skills_dir / ".archive", "x")
    write_skill(skills_dir, "x")
    ok, msg = u.restore_skill("x")
    assert ok is False and "destination already exists" in msg
    ok, msg = u.restore_skill("y")
    assert ok is False and "not found in archive" in msg


def test_restore_archived_skill(home):
    from curator import skill_usage as u
    write_skill(home / "skills" / ".archive", "b")
    assert u.restore_skill("b")[0] is True
    assert (home / "skills" / "b" / "SKILL.md").exists()


def test_archive_refusal_messages(home):
    from curator import skill_usage as u
    ok, msg = u.archive_skill("absent")
    assert not ok and "not found" in msg


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

def test_curated_report_rows_and_persisted_flag(home):
    from curator import skill_usage as u
    skills_dir = home / "skills"
    write_skill(skills_dir, "managed")
    write_skill(skills_dir, "unmanaged")
    u.mark_agent_created("managed")
    rows = {r["name"]: r for r in u.curated_report()}
    assert set(rows) == {"managed"}
    assert rows["managed"]["_persisted"] is True
    assert rows["managed"]["owner"] == "managed"
    assert "last_activity_at" in rows["managed"] and "activity_count" in rows["managed"]


def test_curated_report_includes_pinned_unmanaged_but_not_ghosts(home):
    from curator import skill_usage as u
    skills_dir = home / "skills"
    write_skill(skills_dir, "legacy")
    assert u.set_pinned("legacy", True) is True
    u.save_usage({**u.load_usage(), "ghost": {**u._empty_record(), "pinned": True}})
    names = [r["name"] for r in u.curated_report()]
    assert names == ["legacy"]


def test_usage_report_covers_all_owners_including_external(home, tmp_path, set_config):
    from curator import skill_usage as u
    skills_dir = home / "skills"
    write_skill(skills_dir, "a")
    write_skill(skills_dir, "mine")
    u.mark_agent_created("a")
    real = write_skill(tmp_path / "ext", "shared")
    write_skill(tmp_path / "ext", "linked")
    os.symlink(tmp_path / "ext" / "linked", skills_dir / "linked")
    set_config({"skills": {"external_dirs": [str(tmp_path / "ext")]}})
    rows = {r["name"]: r["owner"] for r in u.usage_report()}
    assert rows == {"a": "managed", "mine": "user", "shared": "external", "linked": "user"}
    assert u.owner("a") == "managed" and u.owner("shared") == "external" and u.owner("linked") == "user"


def test_telemetry_provenance_labels(home, set_config, tmp_path):
    from curator import skill_usage as u
    skills_dir = home / "skills"
    write_skill(skills_dir, "local")
    ext = tmp_path / "ext"
    write_skill(ext, "ext-skill")
    set_config({"skills": {"external_dirs": [str(ext)]}})
    assert u.telemetry_provenance("x", {"created_by": "agent"}) == "agent_created"
    assert u.telemetry_provenance("ext-skill") == "external"
    assert u.telemetry_provenance("local") == "local"
    assert u.telemetry_provenance("nowhere") == "unknown"


# ---------------------------------------------------------------------------
# Telemetry vs curation — usage is tracked for ALL skills; curation is not
# ---------------------------------------------------------------------------

def test_end_to_end_telemetry_tracked_but_lifecycle_refused(home, tmp_path, set_config):
    from curator.skill_usage import (
        STATE_ACTIVE, STATE_ARCHIVED, STATE_STALE, archive_skill, bump_patch, bump_use, bump_view,
        load_usage, set_pinned, set_state)
    skills_dir = home / "skills"
    write_skill(skills_dir, "mine")
    write_skill(tmp_path / "ext", "ext-one")
    set_config({"skills": {"external_dirs": [str(tmp_path / "ext")]}})

    for name in ("ext-one",):
        bump_view(name)
        bump_use(name)
        bump_patch(name)
        set_state(name, STATE_STALE)
        set_state(name, STATE_ARCHIVED)
        assert set_pinned(name, True) is False
        ok, _msg = archive_skill(name)
        assert not ok

    data = load_usage()
    for name in ("ext-one",):
        assert data[name]["view_count"] == 1
        assert data[name]["use_count"] == 1
        assert data[name]["patch_count"] == 1
        assert data[name]["state"] == STATE_ACTIVE
        assert data[name]["archived_at"] is None
        assert data[name]["pinned"] is False
        assert data[name].get("created_by") != "agent"
    assert (tmp_path / "ext" / "ext-one" / "SKILL.md").exists()
    bump_view("mine")
    assert load_usage()["mine"]["view_count"] == 1


# ---------------------------------------------------------------------------
# Unmanaged enumeration + adoption
# ---------------------------------------------------------------------------

def _seed_usage(skills_dir: Path, records: dict) -> None:
    (skills_dir / ".usage.json").write_text(json.dumps(records, indent=1), encoding="utf-8")


def test_unmanaged_report_explains_why(home):
    from curator import skill_usage as u
    skills_dir = home / "skills"
    write_skill(skills_dir, "legacy")
    write_skill(skills_dir, "foreground")
    write_skill(skills_dir, "managed")
    _seed_usage(skills_dir, {"foreground": {"created_by": None}, "managed": {"created_by": "agent"}})
    rows = {r["name"]: r for r in u.unmanaged_report()}
    assert set(rows) == {"legacy", "foreground"}
    assert rows["legacy"]["has_provenance_key"] is False and rows["legacy"]["has_record"] is False
    assert rows["foreground"]["has_provenance_key"] is True and rows["foreground"]["has_record"] is True
    assert u.list_unmanaged_skill_names() == ["foreground", "legacy"]


def test_adopt_preserves_the_inactivity_clock(home):
    from curator.skill_usage import adopt_skill, get_record, latest_activity_at
    skills_dir = home / "skills"
    write_skill(skills_dir, "legacy")
    _seed_usage(skills_dir, {"legacy": {
        "use_count": 5, "patch_count": 7,
        "last_used_at": "2026-04-29T00:00:00+00:00", "created_at": "2026-04-28T00:00:00+00:00"}})
    before = latest_activity_at(get_record("legacy"))
    ok, msg = adopt_skill("legacy")
    assert ok is True and "adopted" in msg
    rec = get_record("legacy")
    assert latest_activity_at(rec) == before
    assert rec["use_count"] == 5 and rec["patch_count"] == 7 and rec["created_by"] == "agent"
    assert adopt_skill("legacy") == (True, "'legacy' is already curator-managed")


@pytest.mark.parametrize("kind", ["external", "missing"])
def test_adopt_refuses_skills_the_user_does_not_own(home, tmp_path, set_config, kind):
    from curator.skill_usage import adopt_skill, load_usage
    skills_dir = home / "skills"
    if kind == "external":
        name = "ext-one"
        write_skill(tmp_path / "ext", name)
        set_config({"skills": {"external_dirs": [str(tmp_path / "ext")]}})
    else:
        name = "no-such-skill"
    ok, _msg = adopt_skill(name)
    assert ok is False
    assert load_usage().get(name, {}).get("created_by") != "agent"


def test_adopt_rejects_empty_name(home):
    from curator.skill_usage import adopt_skill
    assert adopt_skill("")[0] is False


def test_set_sync_flag_is_curation_gated(home, tmp_path, set_config):
    from curator import skill_usage as u
    skills_dir = home / "skills"
    write_skill(skills_dir, "mine")
    write_skill(tmp_path / "ext", "b")
    set_config({"skills": {"external_dirs": [str(tmp_path / "ext")]}})
    u.set_sync("mine", True)
    u.set_sync("b", True)
    assert u.is_sync_enabled("mine") is True
    assert u.is_sync_enabled("b") is False


def test_find_skill_dir_matches_by_frontmatter_name(home):
    from curator import skill_usage as u
    d = write_skill(home / "skills", "dir-name", category="cat")
    (d / "SKILL.md").write_text("---\nname: real-name\ndescription: x\n---\n", encoding="utf-8")
    assert u._find_skill_dir("real-name") == d
    assert u._find_skill_dir("dir-name") is None


def test_read_skill_name_falls_back_and_handles_quotes(tmp_path):
    from curator.skill_usage import _read_skill_name
    p = tmp_path / "SKILL.md"
    p.write_text("---\nname: 'quoted name'\n---\n", encoding="utf-8")
    assert _read_skill_name(p, fallback="fb") == "quoted name"
    p.write_text("no frontmatter", encoding="utf-8")
    assert _read_skill_name(p, fallback="fb") == "fb"
    assert _read_skill_name(tmp_path / "missing.md", fallback="fb") == "fb"


def test_symlinked_skill_dir_is_scanned_by_every_scanner(home, tmp_path):
    """~/.claude/skills/x -> ../../.agents/skills/x is common; Python 3.13+ rglob stops at symlinks
    under ** while os.walk(followlinks=True) does not — both scanners must agree."""
    from curator import skill_usage as u
    real = write_skill(tmp_path / "agents-skills", "linked")
    (home / "skills" / "linked").symlink_to(real, target_is_directory=True)
    assert u._find_skill_dir("linked") is not None
    assert u.adopt_skill("linked")[0] is True
    assert "linked" in u.list_agent_created_skill_names()
    assert [r["name"] for r in u.curated_report()] == ["linked"]
    assert [r["name"] for r in u.usage_report()] == ["linked"]
