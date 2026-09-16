"""curator.skill_ledger — per-mutation audit ledger + rollback."""

from __future__ import annotations

import io
import json
import tarfile
from pathlib import Path

import pytest

VALID_SKILL_CONTENT = """---
name: my-skill
description: test skill
---

# My Skill

Original body.
"""


def _create(name="my-skill", content=VALID_SKILL_CONTENT):
    from curator.skill_manager import skill_manage
    return json.loads(skill_manage(action="create", name=name, content=content))


def _write_skills_tarball(home: Path, files: dict, stamp: str = "2026-08-01T00-00-00Z"):
    from curator import paths
    snap = paths.backups_dir() / stamp
    snap.mkdir(parents=True, exist_ok=True)
    tar_path = snap / "skills.tar.gz"
    with tarfile.open(tar_path, "w:gz") as tf:
        for rel, content in files.items():
            data = content.encode("utf-8") if isinstance(content, str) else content
            info = tarfile.TarInfo(name=rel)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    return tar_path


def test_background_review_patch_ledgers_and_rolls_back(home):
    from curator import skill_ledger
    from curator.skill_manager import skill_manage
    from curator.skill_manager_guards import mark_background_review_skill_read
    from curator.skill_provenance import BACKGROUND_REVIEW, reset_current_write_origin, set_current_write_origin

    token = set_current_write_origin(BACKGROUND_REVIEW)
    try:
        assert _create()["success"] is True
        skill_md = home / "skills" / "my-skill" / "SKILL.md"
        original = skill_md.read_text(encoding="utf-8")
        mark_background_review_skill_read(skill_md)
        patched = json.loads(skill_manage(action="patch", name="my-skill",
                                          old_string="Original body.", new_string="Updated body."))
    finally:
        reset_current_write_origin(token)
    assert patched["success"] is True, patched
    assert "Updated body." in skill_md.read_text(encoding="utf-8")
    rows = [r for r in skill_ledger.list_entries(skill="my-skill") if r["action"] == "patch"]
    assert len(rows) == 1
    assert rows[0]["actor"] == "curator"
    assert any(i["path"].endswith("SKILL.md") for i in rows[0]["before"])
    ok, msg = skill_ledger.rollback_entry(rows[0]["id"])
    assert ok is True, msg
    assert skill_md.read_text(encoding="utf-8") == original


def test_foreground_patch_is_ledgered_as_agent(home):
    from curator import skill_ledger
    from curator.skill_manager import skill_manage
    assert _create()["success"] is True
    patched = json.loads(skill_manage(action="patch", name="my-skill",
                                      old_string="Original body.", new_string="Updated body."))
    assert patched["success"] is True
    rows = [r for r in skill_ledger.list_entries(skill="my-skill") if r["action"] == "patch"]
    assert len(rows) == 1 and rows[0]["actor"] == "agent"


def test_rollback_refuses_paths_outside_home(home):
    from curator import skill_ledger
    entry_id = skill_ledger.append_entry("patch", "evil", before=[{"path": "/etc/passwd", "sha256": "0" * 64}], after=[])
    assert entry_id is not None
    ok, msg = skill_ledger.rollback_entry(entry_id)
    assert ok is False and "outside" in msg


def test_missing_blob_aborts_rollback_before_any_change(home):
    from curator import skill_ledger
    assert _create()["success"] is True
    skill_md = home / "skills" / "my-skill" / "SKILL.md"
    entry_id = skill_ledger.append_entry("patch", "my-skill", before=[{"path": str(skill_md), "sha256": "a" * 64}], after=[])
    current = skill_md.read_bytes()
    ok, msg = skill_ledger.rollback_entry(entry_id)
    assert ok is False and "missing blob" in msg
    assert skill_md.read_bytes() == current


def test_ledger_entry_on_edit_and_delete(home):
    from curator import skill_ledger
    from curator.skill_manager import skill_manage
    assert _create()["success"] is True
    edited = json.loads(skill_manage(action="edit", name="my-skill",
                                     content=VALID_SKILL_CONTENT.replace("Original body.", "Edited body.")))
    assert edited["success"] is True
    deleted = json.loads(skill_manage(action="delete", name="my-skill", absorbed_into=""))
    assert deleted["success"] is True
    actions = [r["action"] for r in skill_ledger.list_entries(skill="my-skill")]
    assert actions == ["delete", "edit", "create"]
    delete_entry = skill_ledger.list_entries(skill="my-skill")[0]
    assert delete_entry["evidence"]["absorbed_into"] == ""
    assert delete_entry["evidence"]["archived"] is False
    assert delete_entry["before"] and delete_entry["after"] == []


def test_deleted_skill_recoverable_from_ledger(home):
    from curator import skill_ledger
    from curator.skill_manager import skill_manage
    assert _create()["success"] is True
    skill_md = home / "skills" / "my-skill" / "SKILL.md"
    original = skill_md.read_bytes()
    assert json.loads(skill_manage(action="delete", name="my-skill"))["success"]
    assert not skill_md.exists()
    entry = skill_ledger.list_entries(skill="my-skill")[0]
    ok, msg = skill_ledger.rollback_entry(entry["id"])
    assert ok is True, msg
    assert skill_md.read_bytes() == original


def test_archive_lands_in_ledger_with_curator_actor(home):
    from curator import skill_ledger, skill_usage
    assert _create()["success"] is True
    tok = skill_ledger.set_ledger_actor("curator")
    try:
        ok, msg = skill_usage.archive_skill("my-skill")
    finally:
        skill_ledger.reset_ledger_actor(tok)
    assert ok, msg
    rows = [r for r in skill_ledger.list_entries(skill="my-skill") if r["action"] == "archive"]
    assert len(rows) == 1 and rows[0]["actor"] == "curator"
    assert rows[0]["before"] and rows[0]["after"]
    ok, msg = skill_usage.restore_skill("my-skill")
    assert ok, msg
    assert any(r["action"] == "restore" for r in skill_ledger.list_entries(skill="my-skill"))


def test_blob_dedupe_same_content_one_blob(home):
    from curator import skill_ledger
    d = home / "skills" / "dedupe-src"
    d.mkdir()
    (d / "a.md").write_text("identical content", encoding="utf-8")
    (d / "b.md").write_text("identical content", encoding="utf-8")
    manifest = skill_ledger.snapshot_paths(d)
    assert len(manifest) == 2
    assert len({m["sha256"] for m in manifest}) == 1
    assert len(list(skill_ledger.blobs_dir().iterdir())) == 1


def test_rollback_fails_closed_when_safety_capture_fails(home, monkeypatch):
    from curator import skill_ledger
    from curator.skill_manager import skill_manage
    assert _create()["success"] is True
    skill_md = home / "skills" / "my-skill" / "SKILL.md"
    assert json.loads(skill_manage(action="patch", name="my-skill", old_string="Original body.",
                                   new_string="Updated body."))["success"]
    entry = [r for r in skill_ledger.list_entries("my-skill") if r["action"] == "patch"][0]
    current = skill_md.read_bytes()
    monkeypatch.setattr(skill_ledger, "append_entry", lambda *a, **k: None)
    ok, msg = skill_ledger.rollback_entry(entry["id"])
    assert ok is False and "safety capture failed" in msg
    assert skill_md.read_bytes() == current


def test_rollback_removes_files_created_by_the_mutation(home):
    from curator import skill_ledger
    from curator.skill_manager import skill_manage
    assert _create()["success"] is True
    wrote = json.loads(skill_manage(action="write_file", name="my-skill", file_path="references/extra.md",
                                    file_content="new supporting file"))
    assert wrote["success"] is True
    extra = home / "skills" / "my-skill" / "references" / "extra.md"
    assert extra.exists()
    entry = [r for r in skill_ledger.list_entries("my-skill") if r["action"] == "write_file"][0]
    ok, msg = skill_ledger.rollback_entry(entry["id"])
    assert ok is True, msg
    assert not extra.exists()


def test_config_gate_off_no_ledger_writes(home, set_config):
    from curator import skill_ledger
    from curator.skill_manager import skill_manage
    set_config({"skills": {"ledger": False}})
    assert _create()["success"] is True
    assert json.loads(skill_manage(action="patch", name="my-skill", old_string="Original body.",
                                   new_string="Updated body."))["success"]
    assert not skill_ledger.ledger_path().exists()
    assert not skill_ledger.blobs_dir().exists()


def test_ledger_failure_never_blocks_the_mutation(home, monkeypatch):
    from curator import skill_ledger
    from curator.skill_manager import skill_manage

    def _boom(*a, **k):
        raise OSError("disk full")
    monkeypatch.setattr(skill_ledger, "snapshot_paths", _boom)
    assert _create()["success"] is True
    assert json.loads(skill_manage(action="patch", name="my-skill", old_string="Original body.",
                                   new_string="Updated body."))["success"]


def test_list_entries_filtering_and_limit(home):
    from curator import skill_ledger
    for i in range(5):
        skill_ledger.append_entry("patch", f"skill-{i % 2}", before=[], after=[])
    assert len(skill_ledger.list_entries(limit=3)) == 3
    only_zero = skill_ledger.list_entries(skill="skill-0")
    assert len(only_zero) == 3 and all(r["skill"] == "skill-0" for r in only_zero)
    assert skill_ledger.get_entry("nope") is None and skill_ledger.get_entry("") is None


def test_malformed_ledger_lines_are_skipped(home):
    from curator import skill_ledger
    skill_ledger.append_entry("patch", "a", before=[], after=[])
    with open(skill_ledger.ledger_path(), "a", encoding="utf-8") as fh:
        fh.write("{not json\n\n")
    skill_ledger.append_entry("patch", "b", before=[], after=[])
    assert [r["skill"] for r in skill_ledger.list_entries()] == ["b", "a"]


def test_user_actor_override_and_derive_actor(home):
    from curator import skill_ledger
    from curator.skill_provenance import BACKGROUND_REVIEW, reset_current_write_origin, set_current_write_origin
    tok = skill_ledger.set_ledger_actor("user")
    try:
        entry_id = skill_ledger.append_entry("archive", "some-skill")
    finally:
        skill_ledger.reset_ledger_actor(tok)
    assert skill_ledger.get_entry(entry_id)["actor"] == "user"
    assert skill_ledger.derive_actor() == "agent"
    t = set_current_write_origin(BACKGROUND_REVIEW)
    try:
        assert skill_ledger.derive_actor() == "curator"
    finally:
        reset_current_write_origin(t)
    assert skill_ledger.get_entry(skill_ledger.append_entry("x", "y", actor="bogus"))["actor"] == "agent"


# ---------------------------------------------------------------------------
# Package-completeness fill from the newest curator backup
# ---------------------------------------------------------------------------

def test_delete_after_rehome_ledgers_full_package_from_backup(home):
    from curator import skill_ledger
    from curator.skill_manager import skill_manage
    assert _create()["success"] is True
    extra = home / "skills" / "my-skill" / "references" / "extra.md"
    assert json.loads(skill_manage(action="write_file", name="my-skill", file_path="references/extra.md",
                                   file_content="roadmap body"))["success"]
    skill_md = home / "skills" / "my-skill" / "SKILL.md"
    _write_skills_tarball(home, {"my-skill/SKILL.md": skill_md.read_text(encoding="utf-8"),
                                 "my-skill/references/extra.md": "roadmap body"})
    extra.unlink()
    extra.parent.rmdir()
    assert json.loads(skill_manage(action="delete", name="my-skill"))["success"]
    delete_entry = [r for r in skill_ledger.list_entries(skill="my-skill") if r["action"] == "delete"][0]
    before_names = {Path(i["path"]).name for i in delete_entry["before"]}
    assert {"SKILL.md", "extra.md"} <= before_names
    ok, msg = skill_ledger.rollback_entry(delete_entry["id"])
    assert ok is True, msg
    assert skill_md.is_file() and extra.read_text(encoding="utf-8") == "roadmap body"


def test_rollback_historical_hollow_entry_restores_full_package(home):
    from curator import skill_ledger
    skill_dir = home / "skills" / "my-skill"
    skill_dir.mkdir()
    skill_md = skill_dir / "SKILL.md"
    skill_md.write_text(VALID_SKILL_CONTENT, encoding="utf-8")
    _write_skills_tarball(home, {"my-skill/SKILL.md": VALID_SKILL_CONTENT, "my-skill/references/roadmap.md": "week 1"})
    skill_md.unlink()
    skill_dir.rmdir()
    entry_id = skill_ledger.append_entry(
        "delete", "my-skill",
        before=[{"path": str(skill_md), "sha256": skill_ledger._store_blob(VALID_SKILL_CONTENT.encode("utf-8"))}],
        after=[])
    ok, msg = skill_ledger.rollback_entry(entry_id)
    assert ok is True, msg
    roadmap = skill_dir / "references" / "roadmap.md"
    assert skill_md.is_file() and roadmap.read_text(encoding="utf-8") == "week 1"


def test_delete_rollback_without_backup_still_works(home):
    from curator import skill_ledger
    from curator.skill_manager import skill_manage
    assert _create()["success"] is True
    skill_md = home / "skills" / "my-skill" / "SKILL.md"
    assert json.loads(skill_manage(action="delete", name="my-skill"))["success"]
    delete_entry = [r for r in skill_ledger.list_entries(skill="my-skill") if r["action"] == "delete"][0]
    assert {Path(i["path"]).name for i in delete_entry["before"]} == {"SKILL.md"}
    ok, msg = skill_ledger.rollback_entry(delete_entry["id"])
    assert ok is True, msg
    assert skill_md.read_text(encoding="utf-8") == VALID_SKILL_CONTENT


def test_backup_fill_does_not_clobber_disk_hash(home):
    from curator import skill_ledger
    skill_dir = home / "skills" / "my-skill"
    skill_dir.mkdir()
    live = VALID_SKILL_CONTENT.replace("Original body.", "Live body.")
    (skill_dir / "SKILL.md").write_text(live, encoding="utf-8")
    _write_skills_tarball(home, {"my-skill/SKILL.md": VALID_SKILL_CONTENT, "my-skill/references/extra.md": "from tar"})
    captured = skill_ledger.snapshot_paths(skill_dir, complete_package=True)
    by_name = {Path(i["path"]).name: i["sha256"] for i in captured}
    assert by_name["SKILL.md"] == skill_ledger._store_blob(live.encode("utf-8"))
    assert by_name["SKILL.md"] != skill_ledger._store_blob(VALID_SKILL_CONTENT.encode("utf-8"))
    assert by_name["extra.md"] == skill_ledger._store_blob(b"from tar")


def test_backup_fill_ignores_tar_path_traversal(home):
    from curator import skill_ledger
    skill_dir = home / "skills" / "my-skill"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(VALID_SKILL_CONTENT, encoding="utf-8")
    _write_skills_tarball(home, {
        "my-skill/SKILL.md": VALID_SKILL_CONTENT, "my-skill/references/legit.md": "legit body",
        "../evil.md": "nope", "my-skill/../outside.md": "nope"})
    captured = skill_ledger.snapshot_paths(skill_dir, complete_package=True)
    paths = [i["path"] for i in captured]
    assert any(p.endswith("references/legit.md") for p in paths)
    assert not any(p.endswith("evil.md") or p.endswith("outside.md") for p in paths)


def test_package_prefixes_shapes(home):
    from curator import skill_ledger
    skills = home / "skills"
    assert skill_ledger.package_prefixes(skills / "cat" / "x", "x") == ["cat/x", "x"]
    from curator import paths
    assert skill_ledger.package_prefixes(paths.archive_dir() / "x-20260101000000", "x-20260101000000") == [
        "x-20260101000000", "x"]
    before = [{"path": str(skills / "y" / "SKILL.md"), "sha256": "0" * 64}]
    assert skill_ledger.package_prefixes(None, "y", before) == ["y"]


def test_rollback_entry_works_when_skills_live_outside_the_state_dir(herdr_layout):
    """Herdr layout: skills in ~/.claude/skills, state elsewhere. Entry paths are validated against the
    skills tree and its archive — not the state dir — so archive + rollback round-trips."""
    from curator import paths, skill_ledger, skill_usage
    from tests.conftest import write_skill
    skills = paths.skills_dir()
    assert skills == herdr_layout / ".claude" / "skills" and not str(skills).startswith(str(paths.state_dir()))
    write_skill(skills, "roam")
    ok, msg = skill_usage.archive_skill("roam")
    assert ok, msg
    entry = next(e for e in skill_ledger.list_entries("roam") if e["action"] == "archive")
    assert any(str(paths.archive_dir()) in i["path"] for i in entry["after"])
    ok, msg = skill_ledger.rollback_entry(entry["id"])
    assert ok, msg
    assert (skills / "roam" / "SKILL.md").exists()
