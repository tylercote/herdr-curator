"""curator.skills_tui — enable/disable, browse and edit skills.

"selected" == enabled, essential skills can never be disabled, categories toggle as a block.
Browse/edit is plugin-added: viewing is a pager, editing spawns $EDITOR on SKILL.md and the
change is ledgered as ``actor=user`` + counted as a patch. The curses layer is a thin renderer
over ``SkillsModel`` / ``Controller`` so everything here runs headless.
"""

from __future__ import annotations

import json

import pytest

from conftest import write_skill


def _model():
    from curator.skills_tui import SkillsModel
    return SkillsModel()


def _cfg(home):
    return json.loads((home / "config.json").read_text(encoding="utf-8"))


# --- model ---------------------------------------------------------------------

def test_rows_merge_usage_disabled_archived_and_managed(home, set_config):
    from curator import skill_usage
    skills = home / "skills"
    write_skill(skills, "alpha", category="cat")
    write_skill(skills, "beta")
    write_skill(skills, "gone")
    skill_usage.adopt_skill("beta")
    skill_usage.bump_use("beta")
    skill_usage.archive_skill("gone")
    set_config({"skills": {"disabled": ["alpha"]}})
    rows = {r["name"]: r for r in _model().rows()}
    assert set(rows) == {"alpha", "beta", "gone"}
    assert rows["alpha"]["enabled"] is False and rows["alpha"]["category"] == "cat" and rows["alpha"]["managed"] is False
    assert rows["beta"]["enabled"] is True and rows["beta"]["managed"] is True and rows["beta"]["use_count"] == 1
    assert rows["gone"]["state"] == "archived" and rows["gone"]["path"].parent.name == ".archive"
    assert rows["alpha"]["description"] == "test skill"


def test_set_enabled_writes_only_the_user_config_layer(home):
    write_skill(home / "skills", "a")
    write_skill(home / "skills", "b")
    m = _model()
    m.set_enabled(["a", "b"], False)
    assert _cfg(home)["skills"]["disabled"] == ["a", "b"]
    assert "interval_hours" not in _cfg(home).get("curator", {})  # defaults are NOT frozen into the file
    m.toggle_enabled("a")
    assert _cfg(home)["skills"]["disabled"] == ["b"]
    assert {r["name"]: r["enabled"] for r in m.rows()} == {"a": True, "b": False}


def test_essential_skills_cannot_be_disabled(home, essential):
    write_skill(home / "skills", essential)
    m = _model()
    m.set_enabled([essential], False)
    assert _cfg(home)["skills"].get("disabled", []) == []
    assert m.rows()[0]["enabled"] is True


def test_platform_scoped_disable_layout(home):
    write_skill(home / "skills", "a")
    m = _model()
    m.set_enabled(["a"], False, platform="discord")
    assert _cfg(home)["skills"]["platform_disabled"] == {"discord": ["a"]}
    assert m.disabled_names() == set() and m.disabled_names(platform="discord") == {"a"}


def test_toggle_category(home):
    skills = home / "skills"
    write_skill(skills, "a", category="devops")
    write_skill(skills, "b", category="devops")
    write_skill(skills, "c")
    m = _model()
    assert m.categories() == ["devops", "uncategorized"]
    m.set_category_enabled("devops", False)
    assert _cfg(home)["skills"]["disabled"] == ["a", "b"]
    m.set_category_enabled("devops", True)
    assert _cfg(home)["skills"]["disabled"] == []
    m.set_category_enabled("uncategorized", False)
    assert _cfg(home)["skills"]["disabled"] == ["c"]


def test_lifecycle_actions_delegate_to_skill_usage(home):
    from curator import skill_usage
    write_skill(home / "skills", "a")
    m = _model()
    assert m.adopt("a")[0] and skill_usage.is_curator_managed("a")
    assert m.pin("a", True)[0] and skill_usage.get_record("a")["pinned"] is True
    ok, msg = m.archive("a")
    assert not ok and "pinned" in msg
    assert m.pin("a", False)[0]
    assert m.archive("a")[0] and skill_usage.list_archived_skill_names() == ["a"]
    assert m.restore("a")[0] and (home / "skills" / "a" / "SKILL.md").exists()


def test_view_returns_skill_md_and_support_files(home):
    d = write_skill(home / "skills", "a", body="# Alpha\nBody.")
    (d / "references").mkdir()
    (d / "references" / "r.md").write_text("ref", encoding="utf-8")
    text = _model().view("a")
    assert "# Alpha" in text and "references/r.md" in text
    assert "not found" in _model().view("ghost")


def test_edit_spawns_editor_and_ledgers_changes_as_user(home, monkeypatch):
    from curator import skill_ledger, skill_usage
    d = write_skill(home / "skills", "a")
    m = _model()
    seen = []

    def fake_editor(path):
        seen.append(path)
        path.write_text(path.read_text(encoding="utf-8") + "\nedited\n", encoding="utf-8")
        return 0
    ok, msg = m.edit("a", editor=fake_editor)
    assert ok and seen == [d / "SKILL.md"] and "edited" in msg
    rows = [r for r in skill_ledger.list_entries("a") if r["action"] == "edit"]
    assert len(rows) == 1 and rows[0]["actor"] == "user"
    assert skill_usage.get_record("a")["patch_count"] == 1
    ok, msg = m.edit("a", editor=lambda p: 0)
    assert ok and "unchanged" in msg and len([r for r in skill_ledger.list_entries("a") if r["action"] == "edit"]) == 1
    assert m.edit("ghost", editor=lambda p: 0)[0] is False


def test_default_editor_resolution(monkeypatch):
    from curator.skills_tui import default_editor_argv
    monkeypatch.setenv("VISUAL", "code --wait")
    monkeypatch.setenv("EDITOR", "vim")
    assert default_editor_argv() == ["code", "--wait"]
    monkeypatch.delenv("VISUAL")
    assert default_editor_argv() == ["vim"]
    monkeypatch.delenv("EDITOR")
    assert default_editor_argv() == ["vi"]


# --- controller (key handling without curses) ---------------------------------

@pytest.fixture
def ctrl(home):
    from curator.skills_tui import Controller, SkillsModel
    skills = home / "skills"
    write_skill(skills, "alpha", category="devops")
    write_skill(skills, "beta", category="devops")
    write_skill(skills, "gamma")
    return Controller(SkillsModel())


def test_controller_navigation_and_filter(ctrl):
    assert [r["name"] for r in ctrl.visible()] == ["gamma", "alpha", "beta"]  # (category, name) order like skills_list
    ctrl.handle_key("j")
    ctrl.handle_key("KEY_DOWN")
    assert ctrl.current()["name"] == "beta"
    ctrl.handle_key("KEY_DOWN")  # clamps
    assert ctrl.current()["name"] == "beta"
    ctrl.handle_key("k")
    assert ctrl.current()["name"] == "alpha"
    ctrl.handle_key("/")
    for ch in "gam":
        ctrl.handle_key(ch)
    ctrl.handle_key("\n")
    assert [r["name"] for r in ctrl.visible()] == ["gamma"] and ctrl.filter_text == "gam"
    ctrl.handle_key("KEY_ESC")
    assert ctrl.filter_text == "" and len(ctrl.visible()) == 3


def test_controller_toggle_pin_adopt_archive_restore(ctrl, home):
    from curator import skill_usage
    ctrl.handle_key(" ")
    assert ctrl.current()["enabled"] is False and _cfg(home)["skills"]["disabled"] == ["gamma"]
    ctrl.handle_key(" ")
    assert ctrl.current()["enabled"] is True
    ctrl.handle_key("a")
    assert ctrl.current()["managed"] is True
    ctrl.handle_key("p")
    assert ctrl.current()["pinned"] is True and "pinned" in ctrl.status
    ctrl.handle_key("x")
    assert "pinned" in ctrl.status and ctrl.current()["state"] == "active"
    ctrl.handle_key("p")
    ctrl.handle_key("x")
    assert ctrl.current()["state"] == "archived" and skill_usage.list_archived_skill_names() == ["gamma"]
    ctrl.handle_key("r")
    assert ctrl.current()["state"] == "active"


def test_controller_category_toggle_and_quit(ctrl, home):
    ctrl.handle_key("KEY_DOWN")  # alpha (devops)
    ctrl.handle_key("c")
    assert _cfg(home)["skills"]["disabled"] == ["alpha", "beta"]
    ctrl.handle_key("c")
    assert _cfg(home)["skills"]["disabled"] == []
    assert ctrl.handle_key("q") is False
    assert ctrl.handle_key("?") is True and ctrl.mode == "help"
    ctrl.handle_key("KEY_ESC")
    assert ctrl.mode == "list"


def test_controller_view_mode_scrolls(ctrl):
    ctrl.handle_key("\n")
    assert ctrl.mode == "view" and ctrl.view_lines and ctrl.view_offset == 0
    ctrl.handle_key("KEY_DOWN")
    assert ctrl.view_offset == 1
    ctrl.handle_key("q")
    assert ctrl.mode == "list"


def test_controller_edit_uses_injected_editor(home, monkeypatch):
    from curator.skills_tui import Controller, SkillsModel
    write_skill(home / "skills", "a")
    calls = []
    c = Controller(SkillsModel(), editor=lambda p: calls.append(p) or 0)
    c.handle_key("e")
    assert calls and calls[0].name == "SKILL.md" and "unchanged" in c.status


def test_render_lines_fit_width(ctrl):
    lines = ctrl.render_lines(width=60, height=8)
    assert len(lines) <= 8 and all(len(l) <= 60 for l in lines)
    assert any("gamma" in l for l in lines) and lines[0].startswith("Skills")


# --- CLI surface ------------------------------------------------------------------

def test_skills_cli_list_enable_disable(home, capsys, essential):
    from curator.skills_tui import cli_main
    write_skill(home / "skills", "a")
    write_skill(home / "skills", "b", category="c")
    assert cli_main(["--list"]) == 0
    out = capsys.readouterr().out
    assert "a" in out and "b" in out and "enabled" in out.lower()
    assert cli_main(["disable", "a", "b"]) == 0
    assert _cfg(home)["skills"]["disabled"] == ["a", "b"]
    assert cli_main(["enable", "a"]) == 0
    assert _cfg(home)["skills"]["disabled"] == ["b"]
    assert cli_main(["disable", essential]) == 1
    assert "essential" in capsys.readouterr().out
    assert cli_main(["--list", "--json"]) == 0
    rows = json.loads(capsys.readouterr().out)
    assert {r["name"]: r["enabled"] for r in rows} == {"a": True, "b": False}


def test_skills_cli_falls_back_when_not_a_tty(home, capsys, monkeypatch):
    from curator.skills_tui import cli_main
    write_skill(home / "skills", "a")
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    assert cli_main([]) == 1
    assert "interactive" in capsys.readouterr().out.lower()


def test_main_routes_skills_verb_and_manifest_has_skills_pane(home):
    from curator import herdr
    from curator.__main__ import _EXTRA
    assert "skills" in _EXTRA
    assert "skills" in herdr.PANES and "skills" in herdr.ACTIONS
