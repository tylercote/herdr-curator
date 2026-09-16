"""curator.integrations — telemetry hooks from host agents (claude / opencode / pi).

The shims forward raw tool calls; ``normalize`` decides whether they concern a skill the
curator scans. Installers are idempotent, tagged, and leave foreign config alone.
"""

from __future__ import annotations

import io
import json
import os
from pathlib import Path

import pytest

from curator import paths
from tests.conftest import write_skill


@pytest.fixture
def integ(home, monkeypatch, tmp_path):
    """Skills tree + host config roots all under tmp: ~/.claude, XDG config, ~/.pi."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg-config"))
    from curator import config, integrations
    write_skill(home / "skills", "alpha")
    write_skill(home / "skills", "beta", category="cat")
    (home / "skills" / "alpha" / "references").mkdir()
    (home / "skills" / "alpha" / "references" / "api.md").write_text("api", encoding="utf-8")
    config.clear_cache()
    return integrations


# --- resolution ------------------------------------------------------------------------

def test_resolve_path_maps_skill_md_and_support_files(integ, home):
    s = home / "skills"
    assert integ.resolve_path(s / "alpha" / "SKILL.md") == "alpha"
    assert integ.resolve_path(s / "alpha" / "references" / "api.md") == "alpha"
    assert integ.resolve_path(s / "cat" / "beta" / "SKILL.md") == "beta"
    assert integ.resolve_path(s / "cat" / "beta") == "beta"


def test_resolve_path_ignores_outside_archived_and_missing(integ, home, tmp_path):
    from curator import paths
    write_skill(paths.archive_dir(), "old")
    assert integ.resolve_path(paths.archive_dir() / "old" / "SKILL.md") is None
    assert integ.resolve_path(tmp_path / "elsewhere" / "SKILL.md") is None
    assert integ.resolve_path(home / "skills" / "nope" / "SKILL.md") is None
    assert integ.resolve_path("") is None


def test_resolve_path_follows_symlinked_skill_from_either_side(integ, home, tmp_path):
    """pi reports the real path (~/.agents/skills/x/SKILL.md); Claude may report the link."""
    real = write_skill(tmp_path / "agents-skills", "linked")
    os.symlink(real, home / "skills" / "linked")
    assert integ.resolve_path(real / "SKILL.md") == "linked"
    assert integ.resolve_path(home / "skills" / "linked" / "SKILL.md") == "linked"


def test_resolve_name_strips_host_prefixes_and_rejects_unknown(integ):
    assert integ.resolve_name("alpha") == "alpha"
    assert integ.resolve_name("plugin:alpha") == "alpha"
    assert integ.resolve_name("apps/web:beta") == "beta"
    assert integ.resolve_name("ghost") is None
    assert integ.resolve_name("") is None


# --- normalize ---------------------------------------------------------------------------

def test_normalize_claude_code_payloads(integ, home):
    s = str(home / "skills")
    assert integ.normalize({"tool_name": "Skill", "tool_input": {"skill": "alpha"}, "session_id": "s1"}) == ("load", "alpha", "s1")
    assert integ.normalize({"tool_name": "Read", "tool_input": {"file_path": f"{s}/alpha/SKILL.md"}}) == ("load", "alpha", None)
    assert integ.normalize({"tool_name": "Edit", "tool_input": {"file_path": f"{s}/cat/beta/SKILL.md", "old_string": "a"}}) == ("patch", "beta", None)
    assert integ.normalize({"tool_name": "Write", "tool_input": {"file_path": f"{s}/alpha/references/api.md"}}) == ("patch", "alpha", None)
    assert integ.normalize({"tool_name": "Read", "tool_input": {"file_path": "/etc/hosts"}}) is None
    # Bash is not in the Claude matcher, but a shell read of a SKILL.md is a load if it ever arrives (same rule as Codex)
    assert integ.normalize({"tool_name": "Bash", "tool_input": {"command": f"cat {s}/alpha/SKILL.md"}}) == ("load", "alpha", None)
    assert integ.normalize({"tool_name": "Bash", "tool_input": {"command": "git status"}}) is None
    assert integ.normalize({"tool_name": "Skill", "tool_input": {"skill": "ghost"}}) is None


def test_normalize_shim_payloads_opencode_and_pi(integ, home):
    s = str(home / "skills")
    assert integ.normalize({"tool": "skill", "args": {"name": "beta"}, "session": "oc1"}) == ("load", "beta", "oc1")
    assert integ.normalize({"tool": "read", "args": {"filePath": f"{s}/alpha/SKILL.md"}}) == ("load", "alpha", None)
    assert integ.normalize({"tool": "read", "args": {"path": f"{s}/alpha/SKILL.md"}}) == ("load", "alpha", None)
    assert integ.normalize({"tool": "edit", "args": {"path": f"{s}/alpha/SKILL.md"}}) == ("patch", "alpha", None)
    assert integ.normalize("garbage") is None
    assert integ.normalize({}) is None


# --- receiver ----------------------------------------------------------------------------

def test_hook_main_records_load_as_view_plus_use_and_patch(integ, home):
    from curator import skill_usage
    s = str(home / "skills")
    assert integ.hook_main("claude", io.StringIO(json.dumps({"tool_name": "Skill", "tool_input": {"skill": "alpha"}}))) == 0
    assert integ.hook_main("pi", io.StringIO(json.dumps({"tool": "edit", "args": {"path": f"{s}/alpha/SKILL.md"}}))) == 0
    rec = skill_usage.load_usage()["alpha"]
    assert rec["view_count"] == 1 and rec["use_count"] == 1 and rec["patch_count"] == 1
    log = integ.hooks_log_path().read_text(encoding="utf-8")
    assert "claude load alpha" in log and "pi patch alpha" in log


def test_hook_main_never_fails_the_host(integ, home):
    assert integ.hook_main("claude", io.StringIO("not json")) == 0
    assert integ.hook_main("claude", io.StringIO("")) == 0
    assert integ.hook_main("opencode", io.StringIO(json.dumps({"tool": "read", "args": {"filePath": "/nowhere"}}))) == 0
    from curator import skill_usage
    assert "alpha" not in skill_usage.load_usage() or skill_usage.load_usage()["alpha"].get("use_count", 0) == 0


# --- installers --------------------------------------------------------------------------

def test_claude_install_is_idempotent_and_preserves_foreign_hooks(integ):
    path = integ.claude_settings_path()
    path.parent.mkdir(parents=True)
    existing = {"model": "opus", "hooks": {"PreToolUse": [{"matcher": "Edit|Write", "hooks": [{"type": "command", "command": "guard.sh"}]}],
                                           "PostToolUse": [{"matcher": "Edit|Write", "hooks": [{"type": "command", "command": "post.sh"}]}]}}
    path.write_text(json.dumps(existing), encoding="utf-8")
    integ.install_claude(); integ.install_claude()
    data = json.loads(path.read_text(encoding="utf-8"))
    post = data["hooks"]["PostToolUse"]
    ours = [e for e in post if integ._is_our_claude_entry(e)]
    assert len(ours) == 1 and ours[0]["matcher"] == "Skill|Read|Edit|Write"
    assert ours[0]["hooks"][0]["command"] == integ.hook_command("claude")
    assert data["model"] == "opus" and data["hooks"]["PreToolUse"] == existing["hooks"]["PreToolUse"]
    assert [e for e in post if not integ._is_our_claude_entry(e)] == existing["hooks"]["PostToolUse"]
    assert integ.status_claude()[0] is True
    integ.uninstall_claude()
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["hooks"]["PostToolUse"] == existing["hooks"]["PostToolUse"]
    assert integ.status_claude()[0] is False


def test_claude_uninstall_drops_empty_containers_and_creates_file_on_install(integ):
    path = integ.claude_settings_path()
    assert not path.exists()
    integ.install_claude()
    assert json.loads(path.read_text(encoding="utf-8"))["hooks"]["PostToolUse"]
    integ.uninstall_claude()
    assert json.loads(path.read_text(encoding="utf-8")) == {}


def test_claude_status_flags_stale_path(integ):
    path = integ.claude_settings_path()
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"hooks": {"PostToolUse": [{"matcher": "Skill", "hooks": [
        {"type": "command", "command": 'python3 "/old/checkout/bin/curator" hook claude'}]}]}}), encoding="utf-8")
    installed, line = integ.status_claude()
    assert installed and "STALE" in line


def test_file_installers_render_bin_path_and_only_remove_their_own(integ, tmp_path):
    for host, target in (("opencode", integ.opencode_plugin_path()), ("pi", integ.pi_extension_path())):
        assert str(target).startswith(str(tmp_path))
        assert integ._install_file(host, target).startswith(f"{host}:")
        text = target.read_text(encoding="utf-8")
        assert str(integ.launcher_path()) in text and integ.MARK in text and "__CURATOR_HOOK__" not in text
        assert integ._status_file(host, target)[0] is True
        target.write_text(text.replace(str(integ.launcher_path()), "/stale/curator-hook"), encoding="utf-8")
        assert "STALE" in integ._status_file(host, target)[1]
        assert integ._uninstall_file(host, target).startswith(f"{host}:") and not target.exists()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("export const Mine = 1\n", encoding="utf-8")
        assert "not ours" in integ._uninstall_file(host, target) and target.exists()
        assert integ._status_file(host, target)[0] is False
        with pytest.raises(RuntimeError, match="not ours"):  # install is as careful as uninstall
            integ._install_file(host, target)
        assert target.read_text(encoding="utf-8") == "export const Mine = 1\n"


def test_opencode_shim_hooks_and_pi_shim_events_are_present(integ):
    oc = integ._render_template("opencode")
    assert '"tool.execute.before"' in oc and '"tool.execute.after"' in oc and 'export const CuratorPlugin: Plugin' in oc
    pi = integ._render_template("pi")
    assert '"tool_execution_start"' in pi and '"tool_execution_end"' in pi and "export default function" in pi
    for text in (oc, pi):
        assert "node:child_process" in text and "isError" in text or "callID" in text


def test_hooks_main_defaults_to_detected_hosts_and_reports(integ, monkeypatch, tmp_path):
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".pi" / "agent").mkdir(parents=True)
    monkeypatch.setattr(integ.shutil, "which", lambda name: None)
    out = io.StringIO()
    assert integ.hooks_main(["status"], out) == 0
    text = out.getvalue()
    assert "claude:" in text and "pi:" in text and "opencode:" not in text and "log:" in text
    out = io.StringIO()
    assert integ.hooks_main(["install"], out) == 0
    assert integ.claude_settings_path().exists() and integ.pi_extension_path().exists() and not integ.opencode_plugin_path().exists()
    out = io.StringIO()
    assert integ.hooks_main(["install", "opencode"], out) == 0 and integ.opencode_plugin_path().exists()
    out = io.StringIO()
    assert integ.hooks_main(["uninstall", "all"], out) == 0
    assert not integ.pi_extension_path().exists() and not integ.opencode_plugin_path().exists()
    assert json.loads(integ.claude_settings_path().read_text(encoding="utf-8")) == {}


def test_hooks_main_with_no_hosts_detected(integ, monkeypatch):
    monkeypatch.setattr(integ.shutil, "which", lambda name: None)
    out = io.StringIO()
    assert integ.hooks_main(["status"], out) == 1 and "no supported host" in out.getvalue()


# --- codex -------------------------------------------------------------------------------

def test_normalize_codex_shell_reads_are_loads_absolute_relative_and_tilde(integ, home, monkeypatch):
    s = home / "skills"
    assert integ.normalize({"tool_name": "shell", "tool_input": {"command": ["bash", "-lc", f"cat {s}/alpha/SKILL.md"]}}) == ("load", "alpha", None)
    assert integ.normalize({"tool_name": "exec_command", "tool_input": {"cmd": f"sed -n '1,40p' {s}/cat/beta/SKILL.md"}, "session_id": "cx"}) == ("load", "beta", "cx")
    assert integ.normalize({"tool_name": "local_shell", "tool_input": {"command": "cat skills/alpha/references/api.md"}, "cwd": str(home)}) == ("load", "alpha", None)
    assert integ.normalize({"tool_name": "shell", "tool_input": {"command": "cat ~/skills/alpha/SKILL.md"}}) is None  # ~ is tmp, no such tree
    assert integ.normalize({"tool_name": "shell", "tool_input": {"command": "ls -la /tmp && git status"}}) is None
    assert integ.normalize({"tool_name": "shell", "tool_input": {"command": ""}}) is None


def test_normalize_codex_redirect_into_skill_is_a_patch(integ, home):
    s = home / "skills"
    assert integ.normalize({"tool_name": "shell", "tool_input": {"command": f"printf 'x' > {s}/alpha/SKILL.md"}}) == ("patch", "alpha", None)
    assert integ.normalize({"tool_name": "shell", "tool_input": {"command": f"cat {s}/alpha/SKILL.md 2>/dev/null"}}) == ("load", "alpha", None)


def test_normalize_codex_apply_patch_uses_file_headers(integ, home):
    patch = "*** Begin Patch\n*** Update File: skills/cat/beta/SKILL.md\n@@\n-a\n+b\n*** End Patch\n"
    assert integ.normalize({"tool_name": "apply_patch", "tool_input": {"patch": patch}, "cwd": str(home)}) == ("patch", "beta", None)
    absolute = f"*** Begin Patch\n*** Add File: {home}/skills/alpha/references/new.md\n+hi\n*** End Patch\n"
    assert integ.normalize({"tool_name": "apply_patch", "tool_input": {"input": absolute}}) == ("patch", "alpha", None)
    assert integ.normalize({"tool_name": "apply_patch", "tool_input": {"patch": "*** Update File: src/main.py\n"}, "cwd": str(home)}) is None


def test_normalize_codex_prompt_mentions_load_known_skills_only(integ):
    assert integ.normalize({"hook_event_name": "UserPromptSubmit", "prompt": "please use $beta for this", "session_id": "p1"}) == ("load", "beta", "p1")
    assert integ.normalize({"hook_event_name": "UserPromptSubmit", "prompt": "costs $5 and $ghost is unknown, $alpha works"}) == ("load", "alpha", None)
    assert integ.normalize({"hook_event_name": "UserPromptSubmit", "prompt": "no mentions here"}) is None
    assert integ.normalize({"hook_event_name": "UserPromptSubmit", "prompt": "Use $beta. Then stop"}) == ("load", "beta", None)
    assert integ.normalize({"hook_event_name": "UserPromptSubmit", "prompt": "try ($alpha), ok?"}) == ("load", "alpha", None)


def test_codex_installer_writes_both_events_idempotently(integ, tmp_path):
    path = integ.codex_hooks_path()
    assert str(path).startswith(str(tmp_path)) and not path.exists()
    integ.install_json_hooks("codex"); integ.install_json_hooks("codex")
    data = json.loads(path.read_text(encoding="utf-8"))
    assert set(data["hooks"]) == {"PostToolUse", "UserPromptSubmit"}
    assert len(data["hooks"]["PostToolUse"]) == 1 and len(data["hooks"]["UserPromptSubmit"]) == 1
    assert "apply_patch" in data["hooks"]["PostToolUse"][0]["matcher"] and "matcher" not in data["hooks"]["UserPromptSubmit"][0]
    assert data["hooks"]["PostToolUse"][0]["hooks"][0]["command"] == integ.hook_command("codex")
    assert integ.status_json_hooks("codex")[0] is True
    # a foreign entry on the same event survives
    data["hooks"]["PostToolUse"].insert(0, {"matcher": "shell", "hooks": [{"type": "command", "command": "mine.sh"}]})
    path.write_text(json.dumps(data), encoding="utf-8")
    integ.uninstall_json_hooks("codex")
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data == {"hooks": {"PostToolUse": [{"matcher": "shell", "hooks": [{"type": "command", "command": "mine.sh"}]}]}}
    assert integ.status_json_hooks("codex")[0] is False


def test_codex_status_flags_missing_event_as_stale(integ):
    path = integ.codex_hooks_path()
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"hooks": {"PostToolUse": [{"matcher": "shell", "hooks": [
        {"type": "command", "command": integ.hook_command("codex")}]}]}}), encoding="utf-8")
    installed, line = integ.status_json_hooks("codex")
    assert installed and "STALE" in line


def test_hooks_main_detects_codex(integ, monkeypatch, tmp_path):
    (tmp_path / ".codex").mkdir()
    monkeypatch.setattr(integ.shutil, "which", lambda name: None)
    out = io.StringIO()
    assert integ.hooks_main(["install"], out) == 0 and "codex:" in out.getvalue()
    assert integ.codex_hooks_path().exists() and not integ.claude_settings_path().exists()


# --- launcher --------------------------------------------------------------------------------

def test_launcher_is_written_points_at_plugin_root_and_is_silent_when_plugin_is_gone(integ, home, tmp_path):
    import stat, subprocess
    launcher = integ.write_launcher()
    assert launcher == home / "bin" / "curator-hook"
    assert launcher.stat().st_mode & stat.S_IXUSR
    assert integ.plugin_root_file().read_text(encoding="utf-8").strip() == str(integ.plugin_root())
    text = launcher.read_text(encoding="utf-8")
    assert integ.MARK in text and 'exec python3 "$ROOT/bin/curator" hook "$@"' in text
    assert integ.hook_command("claude") == f'"{launcher}" claude'
    # real plugin root: a payload goes through and is recorded
    s = str(home / "skills")
    r = subprocess.run(["sh", str(launcher), "pi"], input=json.dumps({"tool": "read", "args": {"path": f"{s}/alpha/SKILL.md"}}),
                       capture_output=True, text=True, timeout=60, env={**os.environ, "CURATOR_HOME": str(home)})
    assert r.returncode == 0 and "pi load alpha" in integ.hooks_log_path().read_text(encoding="utf-8")
    # no python3 on PATH (hooks inherit the host's environment): exit 0, no output, nothing recorded
    before = integ.hooks_log_path().read_text(encoding="utf-8")
    r = subprocess.run(["/bin/sh", str(launcher), "pi"], input=json.dumps({"tool": "read", "args": {"path": f"{s}/alpha/SKILL.md"}}),
                       capture_output=True, text=True, timeout=30, env={**os.environ, "PATH": str(tmp_path / "empty-bin")})
    assert r.returncode == 0 and r.stdout == "" and r.stderr == ""
    assert integ.hooks_log_path().read_text(encoding="utf-8") == before
    # plugin gone: exit 0, no output, nothing recorded
    integ.plugin_root_file().write_text(str(tmp_path / "nowhere") + "\n", encoding="utf-8")
    r = subprocess.run(["sh", str(launcher), "pi"], input="{}", capture_output=True, text=True, timeout=30)
    assert r.returncode == 0 and r.stdout == "" and r.stderr == ""


def test_launcher_bakes_herdr_dirs_and_rewrites_only_on_change(integ, monkeypatch, tmp_path):
    integ.write_launcher()
    first = integ.launcher_path().stat().st_mtime_ns
    integ.write_launcher()
    assert integ.launcher_path().stat().st_mtime_ns == first
    monkeypatch.setenv("HERDR_PLUGIN_STATE_DIR", str(tmp_path / "hstate"))
    monkeypatch.setenv("HERDR_PLUGIN_CONFIG_DIR", str(tmp_path / "hcfg"))
    launcher = integ.write_launcher()
    assert launcher == tmp_path / "hstate" / "bin" / "curator-hook"
    text = launcher.read_text(encoding="utf-8")
    assert f'export HERDR_PLUGIN_STATE_DIR="{tmp_path / "hstate"}"' in text and 'export HERDR_PLUGIN_CONFIG_DIR=' in text


def test_installed_configs_reference_the_launcher_not_the_checkout(integ):
    integ.install_json_hooks("claude"); integ.install_json_hooks("codex")
    integ._install_file("pi", integ.pi_extension_path()); integ._install_file("opencode", integ.opencode_plugin_path())
    launcher = str(integ.launcher_path())
    for path in (integ.claude_settings_path(), integ.codex_hooks_path(), integ.pi_extension_path(), integ.opencode_plugin_path()):
        text = path.read_text(encoding="utf-8")
        assert launcher in text and str(integ.curator_bin()) not in text, path
    assert integ._is_our_command(f'"{launcher}" codex', "codex")
    assert integ._is_our_command('python3 "/old/bin/curator" hook codex', "codex")  # pre-launcher installs are still ours
    assert not integ._is_our_command(f'"{launcher}" claude', "codex")
    assert not integ._is_our_command("guard.sh codex", "codex")


def test_uninstall_all_removes_launcher(integ, monkeypatch, tmp_path):
    (tmp_path / ".pi" / "agent").mkdir(parents=True)
    monkeypatch.setattr(integ.shutil, "which", lambda name: None)
    integ.hooks_main(["install"], io.StringIO())
    assert integ.launcher_path().exists()
    integ.hooks_main(["uninstall", "pi"], io.StringIO())
    assert integ.launcher_path().exists()
    integ.hooks_main(["uninstall"], io.StringIO())
    assert not integ.launcher_path().exists() and not integ.plugin_root_file().exists()


# --- harness skill dirs ----------------------------------------------------------------------

def test_harness_skill_dirs_finds_existing_and_excludes_curated_tree(integ, home, tmp_path, monkeypatch):
    assert integ.harness_skill_dirs() == []
    (tmp_path / ".agents" / "skills").mkdir(parents=True)
    (tmp_path / ".codex" / "skills").mkdir(parents=True)
    (tmp_path / "xdg-config" / "opencode" / "skills").mkdir(parents=True)
    found = integ.harness_skill_dirs()
    assert found == [tmp_path / ".agents" / "skills", tmp_path / ".codex" / "skills", tmp_path / "xdg-config" / "opencode" / "skills"]
    monkeypatch.setenv("CURATOR_SKILLS_DIR", str(tmp_path / ".agents" / "skills"))
    assert tmp_path / ".agents" / "skills" not in integ.harness_skill_dirs()


def test_register_skill_dirs_merges_dedupes_and_is_idempotent(integ, home, tmp_path):
    from curator import config, skill_utils
    write_skill(tmp_path / ".agents" / "skills", "ext-a")
    (tmp_path / ".pi" / "agent" / "skills").mkdir(parents=True)
    config.update_user_config(lambda c: c.setdefault("skills", {}).update({"external_dirs": [str(tmp_path / ".pi" / "agent" / "skills"), "~/other"]}))
    added = integ.register_skill_dirs()
    assert added == ["~/.agents/skills"]
    # the user's config.json is never machine-edited; registrations live in the state dir
    assert config.read_user_config()["skills"]["external_dirs"] == [str(tmp_path / ".pi" / "agent" / "skills"), "~/other"]
    assert json.loads(paths.registered_dirs_file().read_text(encoding="utf-8")) == ["~/.agents/skills"]
    assert integ.register_skill_dirs() == []
    assert tmp_path / ".agents" / "skills" in skill_utils.get_external_skills_dirs()
    assert integ.resolve_path(tmp_path / ".agents" / "skills" / "ext-a" / "SKILL.md") == "ext-a"


def test_skill_linked_into_curated_tree_stays_local_after_registering_its_real_dir(integ, home, tmp_path):
    """The link is the adoption: ~/.claude/skills/x -> ~/.agents/skills/x must not become read-only."""
    from curator import skill_usage, skill_utils
    real = write_skill(tmp_path / ".agents" / "skills", "linked")
    write_skill(tmp_path / ".agents" / "skills", "sibling")
    os.symlink(real, home / "skills" / "linked")
    assert integ.register_skill_dirs() == ["~/.agents/skills"]
    assert not skill_utils.is_external_skill_path(home / "skills" / "linked" / "SKILL.md")
    assert not skill_utils.is_external_skill_path(real / "SKILL.md")
    assert skill_utils.is_external_skill_path(tmp_path / ".agents" / "skills" / "sibling" / "SKILL.md")
    assert skill_usage._find_skill_dir("linked") == home / "skills" / "linked"
    assert skill_usage._find_external_skill_dir("linked") is None
    assert skill_usage._find_external_skill_dir("sibling") is not None
    ok, msg = skill_usage.adopt_skill("linked")
    assert ok, msg
    assert skill_usage.telemetry_provenance("sibling") == "external"


# --- reconcile -------------------------------------------------------------------------------

def test_reconcile_installs_detected_hosts_registers_dirs_and_is_idempotent(integ, monkeypatch, tmp_path):
    (tmp_path / ".claude").mkdir(); (tmp_path / ".codex").mkdir(); (tmp_path / ".agents" / "skills").mkdir(parents=True)
    monkeypatch.setattr(integ.shutil, "which", lambda name: None)
    r = integ.reconcile()
    assert r["installed"] == ["claude", "codex"] and r["registered"] == ["~/.agents/skills"] and r["failed"] == []
    assert integ.launcher_path().exists() and integ.claude_settings_path().exists() and integ.codex_hooks_path().exists()
    notice = integ.reconcile_notice(r)
    assert notice and "claude, codex" in notice[0] and any("/hooks" in l for l in notice) and any(".agents/skills" in l for l in notice)
    r2 = integ.reconcile()
    assert r2["installed"] == [] and r2["registered"] == [] and integ.reconcile_notice(r2) is None
    # a stale entry gets refreshed
    path = integ.claude_settings_path()
    path.write_text(path.read_text(encoding="utf-8").replace(str(integ.launcher_path()), "/old/curator-hook"), encoding="utf-8")
    assert integ.reconcile()["installed"] == ["claude"]


def test_reconcile_respects_auto_off_and_host_subset(integ, monkeypatch, tmp_path, set_config):
    (tmp_path / ".claude").mkdir(); (tmp_path / ".codex").mkdir(); (tmp_path / ".agents" / "skills").mkdir(parents=True)
    monkeypatch.setattr(integ.shutil, "which", lambda name: None)
    set_config({"hooks": {"auto": False, "register_skill_dirs": False}})
    r = integ.reconcile()
    assert r["installed"] == [] and r["registered"] == [] and integ.launcher_path().exists()
    set_config({"hooks": {"auto": True, "hosts": ["codex"], "register_skill_dirs": False}})
    assert integ.reconcile()["installed"] == ["codex"] and not integ.claude_settings_path().exists()


def test_hooks_sync_and_launcher_verbs(integ, monkeypatch, tmp_path):
    (tmp_path / ".pi" / "agent").mkdir(parents=True)
    monkeypatch.setattr(integ.shutil, "which", lambda name: None)
    out = io.StringIO()
    assert integ.hooks_main(["launcher"], out) == 0 and "launcher:" in out.getvalue() and integ.launcher_path().exists()
    out = io.StringIO()
    assert integ.hooks_main(["sync"], out) == 0
    assert "installed: pi" in out.getvalue() and "pi:       installed" in out.getvalue()


def test_symlink_inside_external_dir_pointing_elsewhere_is_still_external(integ, home, tmp_path):
    """~/.agents/skills/x -> ~/.somewhere/x: sits in an external dir, so it is external — not 'user'."""
    import json
    from curator import config, skill_usage, skill_utils
    from curator.skills_tool import skills_list
    real = write_skill(tmp_path / "elsewhere", "roamer")
    (tmp_path / ".agents" / "skills").mkdir(parents=True)
    os.symlink(real, tmp_path / ".agents" / "skills" / "roamer")
    assert integ.register_skill_dirs() == ["~/.agents/skills"]
    assert skill_utils.is_external_skill_path(tmp_path / ".agents" / "skills" / "roamer" / "SKILL.md")
    assert skill_usage.owner("roamer") == "external"
    assert {r["name"]: r["owner"] for r in skill_usage.usage_report()}["roamer"] == "external"
    assert {r["name"]: r["owner"] for r in json.loads(skills_list())["skills"]}["roamer"] == "external"
    assert skill_usage.is_curation_eligible("roamer") is False
    assert skill_usage.adopt_skill("roamer")[0] is False


# --- host config files are rewritten in place, not replaced --------------------------------

def test_install_preserves_existing_mode_and_uses_0644_for_new_files(integ):
    path = integ.claude_settings_path()
    path.parent.mkdir(parents=True)
    path.write_text("{}", encoding="utf-8")
    os.chmod(path, 0o600)
    integ.install_claude()
    assert oct(path.stat().st_mode & 0o777) == "0o600"
    os.chmod(path, 0o644)
    integ.install_claude()
    assert oct(path.stat().st_mode & 0o777) == "0o644"
    codex = integ.codex_hooks_path()
    integ.install_json_hooks("codex")
    umask = os.umask(0); os.umask(umask)
    assert codex.stat().st_mode & 0o777 == 0o644 & ~umask
    integ.install_json_hooks("codex")
    assert codex.stat().st_mode & 0o777 == 0o644 & ~umask


def test_install_writes_through_a_symlinked_config_file(integ, tmp_path):
    """Dotfile managers link ~/.claude/settings.json elsewhere; the link must survive install + uninstall."""
    real = tmp_path / "dotfiles" / "settings.json"
    real.parent.mkdir()
    real.write_text(json.dumps({"model": "opus"}), encoding="utf-8")
    path = integ.claude_settings_path()
    path.parent.mkdir(parents=True)
    os.symlink(real, path)
    integ.install_claude()
    assert path.is_symlink() and os.readlink(path) == str(real)
    data = json.loads(real.read_text(encoding="utf-8"))
    assert data["model"] == "opus" and any(integ._is_our_claude_entry(e) for e in data["hooks"]["PostToolUse"])
    integ.uninstall_claude()
    assert path.is_symlink() and json.loads(real.read_text(encoding="utf-8")) == {"model": "opus"}
    # file installers too
    plugin = integ.opencode_plugin_path()
    real_ts = tmp_path / "dotfiles" / "curator.ts"
    real_ts.write_text(f"// {integ.MARK} — an earlier install of ours\n", encoding="utf-8")
    plugin.parent.mkdir(parents=True)
    os.symlink(real_ts, plugin)
    integ._install_file("opencode", plugin)
    assert plugin.is_symlink() and integ.MARK in real_ts.read_text(encoding="utf-8")


def test_resolve_path_outside_every_skill_root_never_walks_the_trees(integ, home, tmp_path, monkeypatch):
    """The common case (a project file) must be decided from the roots alone."""
    walked = []
    monkeypatch.setattr(integ, "skill_index", lambda: walked.append(1) or {})
    assert integ.resolve_path(tmp_path / "project" / "main.py") is None
    assert integ.resolve_path("/etc/hosts") is None
    assert walked == []
    # a path under a root still consults the index (which is patched to empty here)
    assert integ.resolve_path(home / "skills" / "alpha" / "SKILL.md") is None
    assert walked == [1]
