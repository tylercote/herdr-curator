"""curator.skill_utils — the subset of Hermes ``agent/skill_utils.py`` the curator needs."""

from __future__ import annotations

from pathlib import Path

from conftest import write_skill


def test_parse_frontmatter_basic_and_body():
    from curator.skill_utils import parse_frontmatter
    fm, body = parse_frontmatter("---\nname: a\ndescription: \"quoted: colon\"\ntags: [x, y]\n---\n\n# Body\n")
    assert fm["name"] == "a"
    assert fm["description"] == "quoted: colon"
    assert fm["tags"] == ["x", "y"]
    assert body.strip() == "# Body"


def test_parse_frontmatter_strips_bom_and_handles_missing():
    from curator.skill_utils import parse_frontmatter
    fm, body = parse_frontmatter("﻿---\nname: b\ndescription: d\n---\nbody")
    assert fm["name"] == "b" and body == "body"
    fm, body = parse_frontmatter("no frontmatter here")
    assert fm == {} and body == "no frontmatter here"


def test_parse_frontmatter_nested_metadata_and_multiline_list():
    from curator.skill_utils import parse_frontmatter
    text = (
        "---\nname: c\ndescription: d\nmetadata:\n  hermes:\n    agent_created: true\n"
        "    tags:\n      - one\n      - two\nplatforms:\n  - linux\n---\nx"
    )
    fm, _ = parse_frontmatter(text)
    assert fm["metadata"]["hermes"]["agent_created"] is True
    assert fm["metadata"]["hermes"]["tags"] == ["one", "two"]
    assert fm["platforms"] == ["linux"]


def test_parse_frontmatter_malformed_falls_back_to_key_value_split():
    from curator.skill_utils import parse_frontmatter
    fm, _ = parse_frontmatter("---\nname: d\ndescription: ok\nweird: [unclosed\n---\nbody")
    assert fm["name"] == "d"
    assert fm["description"] == "ok"


def test_is_excluded_skill_path_and_support_dirs(tmp_path):
    from curator.skill_utils import is_excluded_skill_path, EXCLUDED_SKILL_DIRS
    for d in (".git", ".hub", ".archive", ".curator_backups", "node_modules", "__pycache__"):
        assert d in EXCLUDED_SKILL_DIRS
        assert is_excluded_skill_path(tmp_path / d / "x" / "SKILL.md")
    root = tmp_path / "skills"
    write_skill(root, "outer")
    nested = root / "outer" / "references" / "inner"
    nested.mkdir(parents=True)
    (nested / "SKILL.md").write_text("---\nname: inner\ndescription: x\n---\n", encoding="utf-8")
    # A SKILL.md under a skill root's support dir is a support file, not a skill.
    assert is_excluded_skill_path(nested / "SKILL.md", root=root)
    assert not is_excluded_skill_path(root / "outer" / "SKILL.md", root=root)


def test_iter_skill_index_files_prunes_excluded_and_support_dirs(tmp_path):
    from curator.skill_utils import iter_skill_index_files
    root = tmp_path / "skills"
    write_skill(root, "b")
    write_skill(root, "a", category="cat")
    (root / "a-support").mkdir()
    write_skill(root, "outer")
    sup = root / "outer" / "scripts" / "helper"
    sup.mkdir(parents=True)
    (sup / "SKILL.md").write_text("---\nname: helper\n---\n", encoding="utf-8")
    write_skill(root / ".archive", "old")
    write_skill(root / "node_modules", "dep")
    found = [p.relative_to(root).as_posix() for p in iter_skill_index_files(root, "SKILL.md")]
    assert found == sorted(found)
    assert set(found) == {"b/SKILL.md", "cat/a/SKILL.md", "outer/SKILL.md"}


def test_iter_skill_index_files_gates_org_mirror_on_active_marker(tmp_path):
    from curator.skill_utils import iter_skill_index_files
    root = tmp_path / "skills"
    write_skill(root / "_org" / "acme", "shared")
    write_skill(root / "_org" / "other", "theirs")
    assert not list(iter_skill_index_files(root, "SKILL.md"))
    (root / "_org" / ".active_org").write_text("acme\n", encoding="utf-8")
    found = [p.relative_to(root).as_posix() for p in iter_skill_index_files(root, "SKILL.md")]
    assert found == ["_org/acme/shared/SKILL.md"]


def test_external_dirs_from_config_dedupes_and_skips_missing(home, set_config, tmp_path):
    from curator.skill_utils import get_external_skills_dirs, get_all_skills_dirs
    ext = tmp_path / "ext"
    ext.mkdir()
    set_config({"skills": {"external_dirs": [str(ext), str(ext), str(tmp_path / "missing"), str(home / "skills")]}})
    assert get_external_skills_dirs() == [ext.resolve()]
    assert get_all_skills_dirs() == [home / "skills", ext.resolve()]


def test_create_dir_included_in_all_dirs_when_set(home, set_config, tmp_path):
    from curator.skill_utils import get_all_skills_dirs, get_skill_create_dir
    create = tmp_path / "fleet"
    create.mkdir()
    assert get_skill_create_dir() is None
    set_config({"skills": {"create_dir": str(create)}})
    assert get_skill_create_dir() == create.resolve()
    assert get_all_skills_dirs() == [home / "skills", create.resolve()]


def test_is_external_skill_path(home, set_config, tmp_path):
    from curator.skill_utils import is_external_skill_path
    ext = tmp_path / "ext"
    write_skill(ext, "e")
    set_config({"skills": {"external_dirs": [str(ext)]}})
    assert is_external_skill_path(ext / "e" / "SKILL.md")
    assert not is_external_skill_path(home / "skills" / "e" / "SKILL.md")


def test_normalize_skill_lookup_name(home, set_config, tmp_path):
    from curator.skill_utils import normalize_skill_lookup_name
    assert normalize_skill_lookup_name("  foo ") == "foo"
    assert normalize_skill_lookup_name("cat/foo") == "cat/foo"
    # An absolute path outside every trusted root passes through unchanged (skill_view rejects it).
    assert normalize_skill_lookup_name("/cat/foo") == "/cat/foo"
    assert normalize_skill_lookup_name(str(home / "skills" / "cat" / "foo")) == "cat/foo"
    ext = tmp_path / "ext"
    ext.mkdir()
    set_config({"skills": {"external_dirs": [str(ext)]}})
    assert normalize_skill_lookup_name(str(ext / "bar")) == "bar"
    outside = str(tmp_path / "elsewhere" / "skill")
    assert normalize_skill_lookup_name(outside) == outside


def test_description_helpers_and_prompt_limit():
    from curator.skill_utils import (
        SKILL_PROMPT_DESC_LIMIT, extract_skill_description, is_skill_description_truncated_for_prompt)
    assert SKILL_PROMPT_DESC_LIMIT == 60
    short = {"description": "Use when X."}
    assert extract_skill_description(short) == "Use when X."
    assert not is_skill_description_truncated_for_prompt(short)
    long = {"description": "x" * 100}
    assert is_skill_description_truncated_for_prompt(long)
    assert extract_skill_description(long) == "x" * 57 + "..."


def test_platform_matching():
    from curator.skill_utils import skill_matches_platform
    import sys
    assert skill_matches_platform({})
    mine = {"darwin": "macos", "linux": "linux", "win32": "windows"}.get(sys.platform, sys.platform)
    assert skill_matches_platform({"platforms": [mine]})
    assert not skill_matches_platform({"platforms": ["plan9"]})


def test_disabled_skill_names_from_config(home, set_config):
    from curator.skill_utils import get_disabled_skill_names
    set_config({"skills": {"disabled": ["a", "b"], "platform_disabled": {"macos": ["c"]}}})
    names = get_disabled_skill_names()
    assert {"a", "b"} <= names
