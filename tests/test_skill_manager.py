"""curator.skill_manager — the ``skill_manage`` tool surface (port of tests/tools/test_skill_manager_tool.py
subset that the curator relies on: validation, create/edit/patch/delete/write_file/remove_file, the
background-review ownership + read-before-write + consolidation-delete guards, pinned guard, rmtree guard)."""

from __future__ import annotations

import json
from contextvars import copy_context

import pytest

from curator import paths


from conftest import write_skill

VALID_SKILL_CONTENT = """\
---
name: test-skill
description: A test skill for unit testing.
---

# Test Skill

Step 1: Do the thing.
"""

VALID_SKILL_CONTENT_2 = """\
---
name: test-skill
description: Updated description.
---

# Test Skill v2

Step 1: Do the new thing.
"""

LONG_DESC_CONTENT = """\
---
name: long-desc
description: Use when deploying multi-region Kubernetes clusters with custom CNI plugins and service mesh.
---

# Long Desc Skill

Step 1.
"""


def _managed_skill(name: str, content: str, category=None):
    """Create a skill and mark it curator-managed: skill_manage's write guard refuses everything else."""
    from curator.skill_manager import _create_skill
    from curator.skill_usage import mark_agent_created
    result = _create_skill(name, content, category)
    if result["success"]:
        mark_agent_created(name)
    return result

def _skill_content(name: str) -> str:
    return f"---\nname: {name}\ndescription: A test skill for unit testing.\n---\n\n# {name}\n\nStep 1: Do the thing.\n"


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

class TestValidation:
    def test_validate_name(self, home):
        from curator.skill_manager import _validate_name
        assert _validate_name("good-skill_1.0") is None
        assert "required" in _validate_name("")
        assert "Invalid skill name" in _validate_name("Bad Name")
        assert "exceeds" in _validate_name("a" * 65)

    def test_validate_category(self, home):
        from curator.skill_manager import _validate_category
        assert _validate_category(None) is None and _validate_category("  ") is None
        assert _validate_category("devops") is None
        assert "Invalid category" in _validate_category("../escape")
        assert "must be a string" in _validate_category(5)

    def test_validate_frontmatter(self, home):
        from curator.skill_manager import _validate_frontmatter
        assert _validate_frontmatter(VALID_SKILL_CONTENT) is None
        assert _validate_frontmatter("﻿" + VALID_SKILL_CONTENT) is None
        assert "empty" in _validate_frontmatter("   ")
        assert "must start with YAML frontmatter" in _validate_frontmatter("no fm")
        assert "not closed" in _validate_frontmatter("---\nname: x\n")
        assert "must include 'description'" in _validate_frontmatter("---\nname: x\n---\nbody")
        assert "content after the frontmatter" in _validate_frontmatter("---\nname: x\ndescription: d\n---\n\n")
        assert "mapping" in _validate_frontmatter("---\n- a\n- b\n---\nbody")
        assert _validate_frontmatter(LONG_DESC_CONTENT) is None
        assert "60-char system-prompt budget" in _validate_frontmatter(LONG_DESC_CONTENT, new_skill=True)

    def test_validate_file_path(self, home):
        from curator.skill_manager import _validate_file_path
        assert _validate_file_path("references/a.md") is None
        assert _validate_file_path("SKILL.md") is None
        assert "required" in _validate_file_path("")
        assert "traversal" in _validate_file_path("references/../x")
        assert "must be under one of" in _validate_file_path("docs/a.md")
        assert "Provide a file path" in _validate_file_path("references")


# ---------------------------------------------------------------------------
# Core actions
# ---------------------------------------------------------------------------

class TestCreateSkill:
    def test_create_skill(self, home):
        from curator.skill_manager import _create_skill
        result = _create_skill("my-skill", VALID_SKILL_CONTENT)
        assert result["success"] is True and result["path"] == "my-skill"
        assert (home / "skills" / "my-skill" / "SKILL.md").exists()

    def test_create_duplicate_blocked(self, home):
        from curator.skill_manager import _create_skill
        _create_skill("my-skill", VALID_SKILL_CONTENT)
        result = _create_skill("my-skill", VALID_SKILL_CONTENT)
        assert result["success"] is False and "already exists" in result["error"]

    def test_create_rejects_category_traversal(self, home):
        from curator.skill_manager import _create_skill
        result = _create_skill("my-skill", VALID_SKILL_CONTENT, category="../escape")
        assert result["success"] is False and "Invalid category '../escape'" in result["error"]
        assert not (home / "escape").exists()

    def test_create_honours_create_dir(self, home, set_config, tmp_path):
        from curator.skill_manager import _create_skill, _find_skill
        fleet = tmp_path / "fleet"
        fleet.mkdir()
        set_config({"skills": {"create_dir": str(fleet)}})
        result = _create_skill("fleet-skill", VALID_SKILL_CONTENT)
        assert result["success"] is True and (fleet / "fleet-skill" / "SKILL.md").exists()
        assert _find_skill("fleet-skill")["path"] == fleet / "fleet-skill"

    def test_edit_long_desc_still_allowed_with_preview(self, home):
        from curator.skill_manager import _edit_skill
        from curator.skill_utils import extract_skill_description, parse_frontmatter
        _managed_skill("my-skill", VALID_SKILL_CONTENT)
        result = _edit_skill("my-skill", LONG_DESC_CONTENT)
        assert result["success"] is True and "System prompt will show" in result["system_prompt_preview"]
        fm, _ = parse_frontmatter(LONG_DESC_CONTENT)
        assert extract_skill_description(fm) in result["system_prompt_preview"]


class TestEditSkill:
    def test_edit_existing_skill(self, home):
        from curator.skill_manager import _edit_skill
        _managed_skill("my-skill", VALID_SKILL_CONTENT)
        assert _edit_skill("my-skill", VALID_SKILL_CONTENT_2)["success"] is True
        assert "Updated description" in (home / "skills" / "my-skill" / "SKILL.md").read_text()

    def test_edit_existing_skill_by_categorized_path(self, home):
        from curator.skill_manager import _edit_skill
        _managed_skill("my-skill", VALID_SKILL_CONTENT, category="software-development")
        result = _edit_skill("software-development/my-skill", VALID_SKILL_CONTENT_2)
        assert result["success"] is True, result.get("error")
        assert "Updated description" in (home / "skills" / "software-development" / "my-skill" / "SKILL.md").read_text()

    def test_find_skill_accepts_categorized_path_and_bare_name(self, home):
        from curator.skill_manager import _find_skill
        _managed_skill("my-skill", VALID_SKILL_CONTENT, category="mlops")
        assert _find_skill("mlops/my-skill")["path"] == home / "skills" / "mlops" / "my-skill"
        assert _find_skill("my-skill")["path"] == home / "skills" / "mlops" / "my-skill"
        assert _find_skill("nope") is None

    def test_edit_invalid_content_rejected(self, home):
        from curator.skill_manager import _edit_skill
        _managed_skill("my-skill", VALID_SKILL_CONTENT)
        assert _edit_skill("my-skill", "no frontmatter")["success"] is False
        assert "A test skill" in (home / "skills" / "my-skill" / "SKILL.md").read_text()

    def test_edit_missing_skill_lists_alternatives(self, home):
        from curator.skill_manager import _edit_skill
        result = _edit_skill("ghost", VALID_SKILL_CONTENT_2)
        assert result["success"] is False and "not found" in result["error"] and "skills_list" in result["error"]


class TestPatchSkill:
    def test_patch_unique_match(self, home):
        from curator.skill_manager import _patch_skill
        _managed_skill("my-skill", VALID_SKILL_CONTENT)
        result = _patch_skill("my-skill", "Do the thing.", "Do the new thing.")
        assert result["success"] is True and "1 replacement" in result["message"]
        assert "Do the new thing." in (home / "skills" / "my-skill" / "SKILL.md").read_text()

    def test_patch_ambiguous_match_rejected(self, home):
        from curator.skill_manager import _patch_skill
        _managed_skill("my-skill", "---\nname: test-skill\ndescription: A test skill.\n---\n\n# Test\n\nword word\n")
        result = _patch_skill("my-skill", "word", "replaced")
        assert result["success"] is False and "match" in result["error"].lower()
        result = _patch_skill("my-skill", "word", "replaced", replace_all=True)
        assert result["success"] is True and "2 replacements" in result["message"]

    def test_patch_missing_old_string_tells_the_model_how_to_recover(self, home):
        from curator.skill_manager import _patch_skill
        _managed_skill("my-skill", VALID_SKILL_CONTENT)
        result = _patch_skill("my-skill", "", "replacement")
        assert result["success"] is False
        err = result["error"]
        assert "read" in err.lower() and "write_file" in err and "exact" in err.lower()
        assert "new_string is required" in _patch_skill("my-skill", "x", None)["error"]

    def test_patch_no_match_gives_hint_and_preview(self, home):
        from curator.skill_manager import _patch_skill
        _managed_skill("my-skill", VALID_SKILL_CONTENT)
        # (a one-letter typo like "thong" is still matched by the last-resort context_aware strategy)
        result = _patch_skill("my-skill", "Completely different text that is nowhere.", "x")
        assert result["success"] is False and "file_preview" in result
        assert "Could not find a match" in result["error"]
        near = _patch_skill("my-skill", "    Step 1: Do the thing.\n    Step 2: nope\n    Step 3: nope", "x")
        assert near["success"] is False and "Did you mean" in near["error"]

    def test_patch_that_breaks_frontmatter_is_rejected(self, home):
        from curator.skill_manager import _patch_skill
        _managed_skill("my-skill", VALID_SKILL_CONTENT)
        result = _patch_skill("my-skill", "description: A test skill for unit testing.", "")
        assert result["success"] is False and "break SKILL.md structure" in result["error"]

    def test_patch_supporting_file(self, home):
        from curator.skill_manager import _patch_skill, _write_file
        _managed_skill("my-skill", VALID_SKILL_CONTENT)
        _write_file("my-skill", "references/a.md", "old text here")
        assert _patch_skill("my-skill", "old text", "new text", file_path="references/a.md")["success"]
        assert (home / "skills" / "my-skill" / "references" / "a.md").read_text() == "new text here"
        assert "File not found" in _patch_skill("my-skill", "x", "y", file_path="references/missing.md")["error"]

    def test_patch_supporting_file_symlink_escape_blocked(self, home, tmp_path):
        from curator.skill_manager import _patch_skill
        outside_file = tmp_path / "outside.txt"
        outside_file.write_text("old text here")
        _managed_skill("my-skill", VALID_SKILL_CONTENT)
        link = home / "skills" / "my-skill" / "references" / "evil.md"
        link.parent.mkdir(parents=True, exist_ok=True)
        try:
            link.symlink_to(outside_file)
        except OSError:
            pytest.skip("Symlinks not supported")
        result = _patch_skill("my-skill", "old text", "new text", file_path="references/evil.md")
        assert result["success"] is False and "escapes" in result["error"].lower()
        assert outside_file.read_text() == "old text here"


class TestDeleteSkill:
    def test_delete_cleans_empty_category_dir(self, home):
        from curator.skill_manager import _delete_skill
        _managed_skill("my-skill", VALID_SKILL_CONTENT, category="devops")
        assert _delete_skill("my-skill")["success"]
        assert not (home / "skills" / "devops").exists()

    def test_delete_with_absorbed_into_equals_self_or_missing_rejected(self, home):
        from curator.skill_manager import _delete_skill
        _managed_skill("narrow", VALID_SKILL_CONTENT)
        result = _delete_skill("narrow", absorbed_into="narrow")
        assert result["success"] is False and "cannot equal" in result["error"]
        result = _delete_skill("narrow", absorbed_into="ghost-umbrella")
        assert result["success"] is False and "does not exist" in result["error"]
        assert (home / "skills" / "narrow").exists()

    def test_delete_notes_absorption(self, home):
        from curator.skill_manager import _delete_skill
        _managed_skill("narrow", VALID_SKILL_CONTENT)
        _managed_skill("umbrella", VALID_SKILL_CONTENT)
        result = _delete_skill("narrow", absorbed_into="umbrella")
        assert result["success"] and "absorbed into 'umbrella'" in result["message"]


class TestWriteFile:
    def test_write_reference_file(self, home):
        from curator.skill_manager import _write_file
        _managed_skill("my-skill", VALID_SKILL_CONTENT)
        result = _write_file("my-skill", "references/api.md", "# API\nEndpoint docs.")
        assert result["success"] is True and (home / "skills" / "my-skill" / "references" / "api.md").exists()
        assert "Create it first" in _write_file("ghost", "references/a.md", "x")["error"]
        assert "file_content is required" in _write_file("my-skill", "references/a.md", None)["error"]
        big = "x" * (1_048_576 + 1)
        assert "1 MiB" in _write_file("my-skill", "references/big.md", big)["error"]

    def test_write_symlink_escape_blocked(self, home, tmp_path):
        from curator.skill_manager import _write_file
        outside_dir = tmp_path / "outside"
        outside_dir.mkdir()
        _managed_skill("my-skill", VALID_SKILL_CONTENT)
        link = home / "skills" / "my-skill" / "references" / "escape"
        link.parent.mkdir(parents=True, exist_ok=True)
        try:
            link.symlink_to(outside_dir, target_is_directory=True)
        except OSError:
            pytest.skip("Symlinks not supported")
        result = _write_file("my-skill", "references/escape/owned.md", "malicious")
        assert result["success"] is False and "escapes" in result["error"].lower()
        assert not (outside_dir / "owned.md").exists()


class TestRemoveFile:
    def test_remove_existing_file(self, home):
        from curator.skill_manager import _remove_file, _write_file
        _managed_skill("my-skill", VALID_SKILL_CONTENT)
        _write_file("my-skill", "references/api.md", "content")
        assert _remove_file("my-skill", "references/api.md")["success"] is True
        assert not (home / "skills" / "my-skill" / "references").exists()  # empty subdir cleaned
        result = _remove_file("my-skill", "references/api.md")
        assert result["success"] is False and "not found" in result["error"]

    def test_remove_lists_available_files(self, home):
        from curator.skill_manager import _remove_file, _write_file
        _managed_skill("my-skill", VALID_SKILL_CONTENT)
        _write_file("my-skill", "references/a.md", "x")
        result = _remove_file("my-skill", "references/b.md")
        assert result["available_files"] == ["references/a.md"]

    def test_remove_symlink_escape_blocked(self, home, tmp_path):
        from curator.skill_manager import _remove_file
        outside_dir = tmp_path / "outside"
        outside_dir.mkdir()
        outside_file = outside_dir / "keep.txt"
        outside_file.write_text("content")
        _managed_skill("my-skill", VALID_SKILL_CONTENT)
        link = home / "skills" / "my-skill" / "references" / "escape"
        link.parent.mkdir(parents=True, exist_ok=True)
        try:
            link.symlink_to(outside_dir, target_is_directory=True)
        except OSError:
            pytest.skip("Symlinks not supported")
        result = _remove_file("my-skill", "references/escape/keep.txt")
        assert result["success"] is False and "escapes" in result["error"].lower()
        assert outside_file.exists()


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------

class TestSkillManageDispatcher:
    def test_unknown_action_and_required_args(self, home):
        from curator.skill_manager import skill_manage
        assert "Unknown action" in json.loads(skill_manage(action="bogus", name="x"))["error"]
        assert "content is required for 'create'" in json.loads(skill_manage(action="create", name="x"))["error"]
        assert "file_path is required" in json.loads(skill_manage(action="write_file", name="x"))["error"]
        r = json.loads(skill_manage(action="write_file", name="x", file_path="references/a.md"))
        assert "file_content is required" in r["error"]

    def test_patch_shape_errors(self, home):
        from curator.skill_manager import skill_manage
        r = json.loads(skill_manage(action="patch", name="x", content="c", old_string="a", new_string="b"))
        assert "EITHER content" in r["error"]

    def test_create_records_agent_provenance_under_every_origin(self, home):
        """Whatever the origin, a skill_manage create is agent-made and therefore managed — otherwise
        its own creator could never edit it again under the unconditional ownership guard."""
        from curator import skill_usage
        from curator.skill_manager import skill_manage
        from curator.skill_provenance import BACKGROUND_REVIEW, reset_current_write_origin, set_current_write_origin
        assert json.loads(skill_manage(action="create", name="fg", content=VALID_SKILL_CONTENT))["success"]
        assert skill_usage.get_record("fg")["created_by"] == "agent"
        tok = set_current_write_origin(BACKGROUND_REVIEW)
        try:
            assert json.loads(skill_manage(action="create", name="bg", content=VALID_SKILL_CONTENT))["success"]
        finally:
            reset_current_write_origin(tok)
        assert skill_usage.get_record("bg")["created_by"] == "agent"

    def test_patch_bumps_telemetry_and_delete_forgets(self, home):
        from curator import skill_usage
        from curator.skill_manager import skill_manage
        assert json.loads(skill_manage(action="create", name="t", content=VALID_SKILL_CONTENT))["success"]
        assert json.loads(skill_manage(action="patch", name="t", old_string="Do the thing.", new_string="Done."))["success"]
        assert skill_usage.get_record("t")["patch_count"] == 1
        assert json.loads(skill_manage(action="write_file", name="t", file_path="references/x.md", file_content="x"))["success"]
        assert skill_usage.get_record("t")["patch_count"] == 2
        assert json.loads(skill_manage(action="delete", name="t"))["success"]
        assert "t" not in skill_usage.load_usage()

    def test_content_alone_on_patch_is_full_rewrite(self, home):
        from curator.skill_manager import skill_manage
        assert json.loads(skill_manage(action="create", name="t", content=VALID_SKILL_CONTENT))["success"]
        r = json.loads(skill_manage(action="patch", name="t", content=VALID_SKILL_CONTENT_2))
        assert r["success"] and "full rewrite" in r["message"]

    def test_batch_operations_atomic_rollback(self, home):
        from curator.skill_manager import skill_manage
        ops = [{"action": "create", "name": "b1", "content": VALID_SKILL_CONTENT},
               {"action": "write_file", "name": "b1", "file_path": "references/a.md", "file_content": "x"},
               {"action": "patch", "name": "b1", "old_string": "NOPE", "new_string": "y"}]
        r = json.loads(skill_manage(action="", name="", operations=ops))
        assert r["success"] is False and r["failed_index"] == 2 and "rolled back" in r["error"]
        assert not (home / "skills" / "b1").exists()
        ok = json.loads(skill_manage(action="", name="", operations=ops[:2]))
        assert ok["success"] and ok["operations_applied"] == 2
        assert (home / "skills" / "b1" / "references" / "a.md").exists()

    def test_batch_shape_rules(self, home):
        from curator.skill_manager import skill_manage
        assert "non-empty array" in json.loads(skill_manage(action="", name="", operations=[]))["error"]
        assert "SOLE op" in json.loads(skill_manage(action="", name="", operations=[{"action": "delete", "name": "a"}, {"action": "patch", "name": "a"}]))["error"]
        assert "needs an 'action'" in json.loads(skill_manage(action="", name="", operations=[{"name": "a"}]))["error"]
        assert "unknown action" in json.loads(skill_manage(action="", name="", operations=[{"action": "edit", "name": "a"}]))["error"]
        assert "needs a 'name'" in json.loads(skill_manage(action="", name="", operations=[{"action": "create"}]))["error"]
        clobber = [{"action": "create", "name": "c", "content": VALID_SKILL_CONTENT},
                   {"action": "patch", "name": "c", "content": VALID_SKILL_CONTENT_2}]
        assert "silently discard" in json.loads(skill_manage(action="", name="", operations=clobber))["error"]
        assert "capped" in json.loads(skill_manage(action="", name="", operations=[{"action": "patch", "name": "a"}] * 21))["error"]

    def test_batch_delete_routes_to_single_op(self, home):
        from curator.skill_manager import skill_manage
        assert json.loads(skill_manage(action="create", name="d", content=VALID_SKILL_CONTENT))["success"]
        r = json.loads(skill_manage(action="", name="", operations=[{"action": "delete", "name": "d", "absorbed_into": ""}]))
        assert r["success"] and not (home / "skills" / "d").exists()


# ---------------------------------------------------------------------------
# Background-review ownership policy
# ---------------------------------------------------------------------------

def _bg_patch(home, name, old, new):
    from curator.skill_manager import skill_manage
    from curator.skill_manager_guards import mark_background_review_skill_read
    from curator.skill_provenance import BACKGROUND_REVIEW, reset_current_write_origin, set_current_write_origin
    token = set_current_write_origin(BACKGROUND_REVIEW)
    try:
        mark_background_review_skill_read(home / "skills" / name / "SKILL.md")
        return json.loads(skill_manage(action="patch", name=name, old_string=old, new_string=new))
    finally:
        reset_current_write_origin(token)


class TestBackgroundOwnershipPolicyConsistency:
    def test_repeated_identical_write_gets_the_same_answer(self, home):
        from curator.skill_manager import _create_skill
        _create_skill("flip-skill", VALID_SKILL_CONTENT)
        first = _bg_patch(home, "flip-skill", "Do the thing.", "Do the new thing.")
        second = _bg_patch(home, "flip-skill", "Do the thing.", "Do the new thing.")
        assert first["success"] == second["success"] is False
        assert "not curator-managed" in first["error"] and "curator adopt flip-skill" in first["error"]

    def test_guard_resolves_categorized_path_to_the_record_key(self, home):
        """`mlops/x` and `x` are the same skill: ownership is decided by the record, not the spelling."""
        from curator.skill_manager import _create_skill, skill_manage
        _create_skill("cat-skill", VALID_SKILL_CONTENT, category="mlops")
        refused = json.loads(skill_manage(action="patch", name="mlops/cat-skill", old_string="Do the thing.", new_string="x"))
        assert refused["success"] is False and "curator adopt cat-skill" in refused["error"]
        _managed_skill("cat-skill2", VALID_SKILL_CONTENT, category="mlops")
        ok = json.loads(skill_manage(action="patch", name="mlops/cat-skill2", old_string="Do the thing.", new_string="x"))
        assert ok["success"] is True, ok

    def test_foreground_write_to_unmanaged_skill_refused(self, home):
        """The ownership guard does not depend on the write origin: even in-process, with the default
        foreground origin, skill_manage refuses a skill that is not curator-managed."""
        from curator.skill_manager import _create_skill, skill_manage
        _create_skill("no-record", VALID_SKILL_CONTENT)
        res = json.loads(skill_manage(action="patch", name="no-record", old_string="Do the thing.", new_string="Do the new thing."))
        assert res["success"] is False and "not curator-managed" in res["error"] and "curator adopt no-record" in res["error"]
        assert "Do the thing." in (home / "skills" / "no-record" / "SKILL.md").read_text()

    def test_adopted_skill_becomes_writable_by_autonomous_curation(self, home):
        from curator import skill_usage
        from curator.skill_manager import _create_skill
        _create_skill("adopt-me", _skill_content("adopt-me"))  # adopt resolves by frontmatter name
        before = _bg_patch(home, "adopt-me", "Do the thing.", "Do the new thing.")
        assert skill_usage.adopt_skill("adopt-me")[0]
        after = _bg_patch(home, "adopt-me", "Do the thing.", "Do the new thing.")
        assert before["success"] is False and after["success"] is True, after

    def test_background_refuses_pinned_and_external(self, home, set_config, tmp_path):
        from curator import skill_usage
        from curator.skill_manager import _create_skill
        skills = home / "skills"
        for name in ("pinned",):
            _create_skill(name, VALID_SKILL_CONTENT)
            skill_usage.mark_agent_created(name)
        assert skill_usage.set_pinned("pinned", True)
        ext = tmp_path / "ext"
        write_skill(ext, "ext-skill", body="Step 1: Do the thing.")
        set_config({"skills": {"external_dirs": [str(ext)]}})
        assert "pinned" in _bg_patch(home, "pinned", "Do the thing.", "x")["error"]
        r = _bg_patch(home, "ext-skill", "Do the thing.", "x")
        assert r["success"] is False and "external" in r["error"]


class TestPinnedGuard:
    def test_edit_refused_when_pinned(self, home):
        from curator import skill_usage
        from curator.skill_manager import _edit_skill
        _managed_skill("my-skill", VALID_SKILL_CONTENT)
        assert skill_usage.set_pinned("my-skill", True)
        result = _edit_skill("my-skill", VALID_SKILL_CONTENT_2)
        assert result["success"] is False and "pinned" in result["error"] and "curator unpin my-skill" in result["error"]
        assert "A test skill" in (home / "skills" / "my-skill" / "SKILL.md").read_text()

    def test_delete_refuses_pinned(self, home):
        from curator import skill_usage
        from curator.skill_manager import _delete_skill
        _managed_skill("my-skill", VALID_SKILL_CONTENT)
        assert skill_usage.set_pinned("my-skill", True)
        result = _delete_skill("my-skill")
        assert result["success"] is False
        assert "pinned" in result["error"].lower() and "curator unpin my-skill" in result["error"]
        assert (home / "skills" / "my-skill" / "SKILL.md").exists()

    def test_essential_skill_never_deleted(self, home, essential):
        from curator.skill_manager import _delete_skill
        _managed_skill(essential, VALID_SKILL_CONTENT)
        result = _delete_skill(essential)
        assert result["success"] is False and "essential" in result["error"]

    def test_broken_sidecar_fails_open(self, home, monkeypatch):
        from curator import skill_usage
        from curator.skill_manager import _delete_skill
        _managed_skill("my-skill", VALID_SKILL_CONTENT)

        def _boom(name):
            raise RuntimeError("sidecar broken")
        monkeypatch.setattr(skill_usage, "get_record", _boom)
        assert _delete_skill("my-skill")["success"] is True


class TestDeleteSkillRmtreeGuard:
    def test_normal_delete_still_works(self, home):
        from curator.skill_manager import _delete_skill
        _managed_skill("good-skill", VALID_SKILL_CONTENT)
        assert _delete_skill("good-skill", absorbed_into="")["success"] is True
        assert not (home / "skills" / "good-skill").exists()

    def test_symlinked_skill_dir_refused(self, home, tmp_path, monkeypatch):
        from curator import skill_manager
        from curator import skill_usage
        monkeypatch.setattr(skill_usage, "_is_curator_managed_record", lambda rec: True)
        victim = tmp_path / "precious_victim"
        victim.mkdir()
        (victim / "important.txt").write_text("DO NOT DELETE")
        evil = home / "skills" / "evil-skill"
        evil.symlink_to(victim, target_is_directory=True)
        monkeypatch.setattr(skill_manager, "_find_skill", lambda name: {"path": evil})
        result = skill_manager._delete_skill("evil-skill", absorbed_into="")
        assert result["success"] is False and "symlink" in result["error"].lower()
        assert (victim / "important.txt").exists()

    def test_out_of_tree_path_refused(self, home, tmp_path, monkeypatch):
        from curator import skill_manager
        from curator import skill_usage
        monkeypatch.setattr(skill_usage, "_is_curator_managed_record", lambda rec: True)
        outside = tmp_path / "outside_skill"
        outside.mkdir()
        (outside / "SKILL.md").write_text("x")
        monkeypatch.setattr(skill_manager, "_find_skill", lambda name: {"path": outside})
        result = skill_manager._delete_skill("outside", absorbed_into="")
        assert result["success"] is False and "skills root" in result["error"].lower()
        assert outside.exists()

    def test_skills_root_itself_refused(self, home, monkeypatch):
        from curator import skill_manager
        from curator import skill_usage
        monkeypatch.setattr(skill_usage, "_is_curator_managed_record", lambda rec: True)
        monkeypatch.setattr(skill_manager, "_find_skill", lambda name: {"path": home / "skills"})
        result = skill_manager._delete_skill("root", absorbed_into="")
        assert result["success"] is False and "skills root itself" in result["error"]


# ---------------------------------------------------------------------------
# Curator consolidation-pass fail-closed delete guard + read-before-write
# ---------------------------------------------------------------------------

@pytest.fixture
def curator_pass(home, monkeypatch):
    from curator import skill_provenance, skill_usage
    monkeypatch.setattr(skill_usage, "_is_curator_managed_record", lambda rec: True)
    monkeypatch.setattr(skill_provenance, "is_background_review", lambda: True)
    from curator.skill_manager_guards import _reset_background_review_read_marks
    _reset_background_review_read_marks()
    yield home / "skills"
    _reset_background_review_read_marks()


def _create_curator_skill(name: str, content: str):
    from curator.skill_manager import _create_skill
    from curator.skill_usage import mark_agent_created
    result = _create_skill(name, content)
    assert result["success"] is True, result
    mark_agent_created(name)
    return result


class TestCuratorConsolidationDeleteGuard:
    def test_bare_prune_during_curator_pass_refused(self, curator_pass):
        from curator.skill_manager import _delete_skill
        _create_curator_skill("active-skill", VALID_SKILL_CONTENT)
        for absorbed in ("", None):
            result = _delete_skill("active-skill", absorbed_into=absorbed)
            assert result["success"] is False and result.get("_fail_closed") is True
        assert (curator_pass / "active-skill").exists()

    def test_verified_consolidation_archives_recoverably(self, curator_pass):
        from curator import skill_usage
        from curator.skill_manager import _delete_skill, skill_manage
        _create_curator_skill("narrow", _skill_content("narrow"))
        _create_curator_skill("umbrella", _skill_content("umbrella"))
        result = json.loads(skill_manage(action="delete", name="narrow", absorbed_into="umbrella"))
        assert result["success"] is True and result["_archived"] is True and "archived" in result["message"]
        assert (paths.archive_dir() / "narrow").exists() and not (curator_pass / "narrow").exists()
        assert skill_usage.get_record("narrow")["state"] == "archived"  # record kept, not forgotten

    def test_background_review_read_survives_copied_tool_contexts(self, curator_pass):
        from curator.skill_manager import skill_manage
        from curator.skills_tool import skill_view
        _create_curator_skill("reviewed", _skill_content("reviewed"))
        assert json.loads(copy_context().run(skill_view, "reviewed"))["success"] is True
        patched = copy_context().run(skill_manage, action="patch", name="reviewed",
                                     old_string="Step 1: Do the thing.", new_string="Step 1: Do the thing safely.")
        assert json.loads(patched)["success"] is True

    def test_background_review_read_marks_stay_isolated_between_reviews(self, curator_pass):
        from curator.skill_manager import skill_manage
        from curator.skill_manager_guards import _reset_background_review_read_marks
        from curator.skills_tool import skill_view
        _create_curator_skill("reviewed", _skill_content("reviewed"))
        first_review = copy_context()
        _reset_background_review_read_marks()
        second_review = copy_context()
        assert json.loads(first_review.run(skill_view, "reviewed"))["success"] is True
        blocked = json.loads(second_review.run(skill_manage, action="patch", name="reviewed",
                                               old_string="Step 1: Do the thing.", new_string="x"))
        assert blocked["success"] is False and blocked.get("_read_before_write_required") is True

    def test_background_review_support_file_overwrite_requires_that_file_read(self, curator_pass):
        from curator.skill_manager import skill_manage
        from curator.skills_tool import skill_view
        _create_curator_skill("reviewed", _skill_content("reviewed"))
        ref = curator_pass / "reviewed" / "references"
        ref.mkdir()
        (ref / "workflow.md").write_text("old workflow\n", encoding="utf-8")
        assert json.loads(skill_view("reviewed"))["success"] is True
        blocked = json.loads(skill_manage(action="write_file", name="reviewed", file_path="references/workflow.md",
                                          file_content="new workflow\n"))
        assert blocked["success"] is False and blocked.get("_read_before_write_required") is True
        assert json.loads(skill_view("reviewed", "references/workflow.md"))["success"] is True
        allowed = json.loads(skill_manage(action="write_file", name="reviewed", file_path="references/workflow.md",
                                          file_content="new workflow\n"))
        assert allowed["success"] is True, allowed
        # A NEW file needs no prior read.
        assert json.loads(skill_manage(action="write_file", name="reviewed", file_path="references/new.md",
                                       file_content="fresh"))["success"] is True

    def test_remove_file_requires_read(self, curator_pass):
        from curator.skill_manager import skill_manage
        from curator.skills_tool import skill_view
        _create_curator_skill("reviewed", _skill_content("reviewed"))
        (curator_pass / "reviewed" / "references").mkdir()
        (curator_pass / "reviewed" / "references" / "a.md").write_text("x", encoding="utf-8")
        blocked = json.loads(skill_manage(action="remove_file", name="reviewed", file_path="references/a.md"))
        assert blocked.get("_read_before_write_required") is True
        assert json.loads(skill_view("reviewed", "references/a.md"))["success"]
        assert json.loads(skill_manage(action="remove_file", name="reviewed", file_path="references/a.md"))["success"]

    def test_preflight_refuses_before_ledger_capture(self, curator_pass, monkeypatch):
        from curator import skill_ledger, skill_usage
        from curator.skill_manager import skill_manage
        monkeypatch.setattr(skill_usage, "_is_curator_managed_record", lambda rec: False)
        _create_curator_skill("user-owned", VALID_SKILL_CONTENT)
        result = json.loads(skill_manage(action="patch", name="user-owned", old_string="a", new_string="b"))
        assert result["success"] is False and "not curator-managed" in result["error"]
        assert not [r for r in skill_ledger.list_entries("user-owned") if r["action"] == "patch"]


def test_skill_manage_schema_shape(home):
    from curator.skill_manager import SKILL_MANAGE_SCHEMA
    assert SKILL_MANAGE_SCHEMA["name"] == "skill_manage"
    ops = SKILL_MANAGE_SCHEMA["parameters"]["properties"]["operations"]
    assert ops["items"]["properties"]["action"]["enum"] == ["create", "patch", "delete", "write_file", "remove_file"]
    assert SKILL_MANAGE_SCHEMA["parameters"]["required"] == ["operations"]


def _bg_delete(name, absorbed_into):
    from curator.skill_manager import skill_manage
    from curator.skill_provenance import BACKGROUND_REVIEW, reset_current_write_origin, set_current_write_origin
    token = set_current_write_origin(BACKGROUND_REVIEW)
    try:
        return json.loads(skill_manage(action="delete", name=name, absorbed_into=absorbed_into))
    finally:
        reset_current_write_origin(token)


def test_background_pass_can_archive_a_managed_duplicate_into_an_external_skill(home, tmp_path, set_config):
    """The 'already covered elsewhere' move: archive the managed copy, never touch the existing skill."""
    from curator import skill_usage
    from curator.skill_manager import _create_skill
    real = write_skill(tmp_path / "ext", "polish", body="# Polish\n\nOriginal.")
    set_config({"skills": {"external_dirs": [str(tmp_path / "ext")]}})
    _create_skill("polish-lite", "---\nname: polish-lite\ndescription: a narrower copy of polish\n---\n\n# Polish lite\n")
    skill_usage.mark_agent_created("polish-lite")
    r = _bg_delete("polish-lite", "polish")
    assert r["success"] is True and r.get("_archived") is True and "absorbed into 'polish'" in r["message"]
    assert not (home / "skills" / "polish-lite").exists() and (paths.archive_dir() / "polish-lite" / "SKILL.md").exists()
    assert (real / "SKILL.md").read_text(encoding="utf-8").endswith("Original.\n")
    # and the reverse is refused: the pass may not archive the external skill itself
    r = _bg_delete("polish", "polish-lite")
    assert r["success"] is False and "external" in r["error"]
