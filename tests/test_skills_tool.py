"""curator.skills_tool — skills_list / skill_view (the read half of the curator fork's toolset)."""

from __future__ import annotations

import json

from conftest import write_skill


def test_skills_list_empty_and_populated(home):
    from curator.skills_tool import skills_list
    out = json.loads(skills_list())
    assert out["success"] is True and out["skills"] == [] and "No skills found" in out["message"]
    write_skill(home / "skills", "b")
    write_skill(home / "skills", "a", category="cat")
    d = write_skill(home / "skills", "nodesc")
    (d / "SKILL.md").write_text("---\nname: nodesc\n---\n\n# Heading\nFirst body line.\n", encoding="utf-8")
    out = json.loads(skills_list())
    assert [s["name"] for s in out["skills"]] == ["b", "nodesc", "a"]  # sorted by (category, name)
    assert out["categories"] == ["cat"] and out["count"] == 3
    by_name = {s["name"]: s for s in out["skills"]}
    assert by_name["a"]["category"] == "cat" and by_name["b"]["category"] is None
    assert by_name["nodesc"]["description"] == "First body line."
    assert json.loads(skills_list(category="cat"))["count"] == 1


def test_skills_list_skips_disabled_and_external_first_wins(home, set_config, tmp_path):
    from curator.skills_tool import skills_list
    write_skill(home / "skills", "dup", body="local")
    ext = tmp_path / "ext"
    write_skill(ext, "dup", body="external")
    write_skill(ext, "only-ext")
    write_skill(home / "skills", "switched-off")  # NB: a skill literally named "off" parses as YAML false
    set_config({"skills": {"external_dirs": [str(ext)], "disabled": ["switched-off"]}})
    names = [s["name"] for s in json.loads(skills_list())["skills"]]
    assert names == ["dup", "only-ext"]


def test_skill_view_content_linked_files_and_path(home):
    from curator.skills_tool import skill_view
    d = write_skill(home / "skills", "viewme", category="cat", body="# Body\n\nStep 1.")
    (d / "references").mkdir()
    (d / "references" / "api.md").write_text("api docs", encoding="utf-8")
    (d / "scripts").mkdir()
    (d / "scripts" / "run.sh").write_text("#!/bin/sh", encoding="utf-8")
    out = json.loads(skill_view("viewme"))
    assert out["success"] and out["name"] == "viewme" and "Step 1." in out["content"]
    assert out["path"] == "cat/viewme/SKILL.md" and out["skill_dir"] == str(d)
    assert out["linked_files"] == {"references": ["references/api.md"], "scripts": ["scripts/run.sh"]}
    assert "skill_view(name, file_path)" in out["usage_hint"]
    assert json.loads(skill_view("cat/viewme"))["success"]
    sub = json.loads(skill_view("viewme", file_path="references/api.md"))
    assert sub["success"] and sub["content"] == "api docs" and sub["file_path"] == "references/api.md"
    missing = json.loads(skill_view("viewme", file_path="references/nope.md"))
    assert missing["success"] is False and missing["available_files"] == ["references/api.md", "scripts/run.sh"]


def test_skill_view_not_found_lists_available(home):
    from curator.skills_tool import skill_view
    write_skill(home / "skills", "exists")
    out = json.loads(skill_view("ghost"))
    assert out["success"] is False and "not found" in out["error"] and out["available_skills"] == ["exists"]


def test_skill_view_rejects_traversal_and_absolute(home):
    from curator.skills_tool import skill_view
    assert "traversal" in json.loads(skill_view("../x"))["error"]
    assert "relative path" in json.loads(skill_view("/abs/x"))["error"]
    assert "relative path" in json.loads(skill_view("C:\\skills\\x"))["error"]
    write_skill(home / "skills", "s")
    assert "escapes" in json.loads(skill_view("s", file_path="../../outside"))["error"].lower()


def test_skill_view_ambiguous_name_refuses(home, set_config, tmp_path):
    from curator.skills_tool import skill_view
    write_skill(home / "skills", "dup")
    ext = tmp_path / "ext"
    write_skill(ext, "dup")
    set_config({"skills": {"external_dirs": [str(ext)]}})
    out = json.loads(skill_view("dup"))
    assert out["success"] is False and "Ambiguous" in out["error"] and len(out["matches"]) == 2


def test_skill_view_disabled_and_platform(home, set_config):
    from curator.skills_tool import skill_view
    write_skill(home / "skills", "switched-off")
    d = write_skill(home / "skills", "plan9")
    (d / "SKILL.md").write_text("---\nname: plan9\ndescription: x\nplatforms: [plan9]\n---\nbody", encoding="utf-8")
    set_config({"skills": {"disabled": ["switched-off"]}})
    assert "disabled" in json.loads(skill_view("switched-off"))["error"]
    assert "not supported on this platform" in json.loads(skill_view("plan9"))["error"]


def test_skill_view_with_bump_records_view_and_use(home):
    from curator import skill_usage
    from curator.skills_tool import skill_view_with_bump
    write_skill(home / "skills", "counted")
    out = json.loads(skill_view_with_bump({"name": "counted"}))
    assert out["success"]
    rec = skill_usage.get_record("counted")
    assert rec["view_count"] == 1 and rec["use_count"] == 1
    json.loads(skill_view_with_bump({"name": "ghost"}))
    assert "ghost" not in skill_usage.load_usage()


def test_skill_view_marks_background_review_read(home, monkeypatch):
    from curator import skill_provenance
    from curator.skill_manager_guards import _background_review_has_read, _reset_background_review_read_marks
    from curator.skills_tool import skill_view
    d = write_skill(home / "skills", "marked")
    (d / "references").mkdir()
    (d / "references" / "r.md").write_text("r", encoding="utf-8")
    _reset_background_review_read_marks()
    monkeypatch.setattr(skill_provenance, "is_background_review", lambda: True)
    assert not _background_review_has_read(d / "SKILL.md")
    json.loads(skill_view("marked"))
    assert _background_review_has_read(d / "SKILL.md") and not _background_review_has_read(d / "references" / "r.md")
    json.loads(skill_view("marked", "references/r.md"))
    assert _background_review_has_read(d / "references" / "r.md")
    _reset_background_review_read_marks()


def test_tool_schemas(home):
    from curator.skills_tool import SKILLS_LIST_SCHEMA, SKILL_VIEW_SCHEMA
    assert SKILLS_LIST_SCHEMA["name"] == "skills_list"
    assert SKILL_VIEW_SCHEMA["name"] == "skill_view" and SKILL_VIEW_SCHEMA["parameters"]["required"] == ["name"]
