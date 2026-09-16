"""curator.setup_wizard — the interactive first-run flow, driven with scripted answers and a fake
``which``/``subprocess.run`` so no real harness is touched."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from curator import paths
from tests.conftest import write_skill

OPENCODE_MODELS = "opencode/claude-sonnet-5\nopencode/claude-opus-5\nopenai/gpt-5.5\n"


class FakeRun:
    """Answers --version, `opencode models` and the claude verification call; records everything."""

    def __init__(self, *, opencode_models: str = OPENCODE_MODELS, claude_verify_ok: bool = True):
        self.calls = []
        self.opencode_models, self.claude_verify_ok = opencode_models, claude_verify_ok

    def __call__(self, argv, **kw):
        self.calls.append(list(argv))
        if argv[1:] == ["--version"]:
            return subprocess.CompletedProcess(argv, 0, f"{argv[0]} 9.9.9\n", "")
        if argv[:2] == ["opencode", "models"]:
            return subprocess.CompletedProcess(argv, 0, self.opencode_models, "")
        if argv[:2] == ["claude", "-p"]:
            body = json.dumps({"is_error": not self.claude_verify_ok, "result": "OK" if self.claude_verify_ok else "model not available",
                               "model": argv[argv.index("--model") + 1]})
            return subprocess.CompletedProcess(argv, 0, body, "")
        raise AssertionError(f"unexpected command {argv}")


@pytest.fixture
def machine(home, monkeypatch, tmp_path):
    """claude + opencode + codex on PATH, pi absent; ~/.claude etc. under tmp; hooks land under tmp."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg-config"))
    present = {"claude": "/usr/local/bin/claude", "opencode": "/opt/homebrew/bin/opencode", "codex": "/usr/local/bin/codex"}
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude" / "settings.json").write_text(json.dumps({"model": "opus"}), encoding="utf-8")
    (tmp_path / ".codex").mkdir()
    (tmp_path / ".codex" / "config.toml").write_text('model = "gpt-6-astra"\n', encoding="utf-8")
    from curator import integrations
    monkeypatch.setattr(integrations.shutil, "which", lambda n: present.get(n))
    return {"which": present.get, "run": FakeRun(), "tmp": tmp_path}


def _wizard(machine, answers, **kw):
    from curator.setup_wizard import Answers, Wizard
    lines = []
    w = Wizard(input_fn=Answers(answers), print_fn=lines.append, which=machine["which"], run=machine["run"], **kw)
    return w, lines


def test_detect_harnesses_reports_presence_and_version(machine):
    from curator.setup_wizard import detect_harnesses
    found = detect_harnesses(which=machine["which"], run=machine["run"])
    assert [h for h, v in found.items() if v.present] == ["claude", "codex", "opencode"]
    assert found["claude"].version == "claude 9.9.9" and not found["pi"].present and found["pi"].version == ""


def test_available_models_per_harness(machine):
    from curator.setup_wizard import available_models, CLAUDE_ALIASES
    oc = available_models("opencode", run=machine["run"])
    assert oc.models == ["opencode/claude-sonnet-5", "opencode/claude-opus-5", "openai/gpt-5.5"] and oc.source == "opencode models"
    cl = available_models("claude", run=machine["run"])
    assert cl.models[:4] == list(CLAUDE_ALIASES) and cl.configured == "opus" and "no model-list command" in cl.note
    cx = available_models("codex", run=machine["run"])
    assert cx.models == [] and cx.configured == "gpt-6-astra" and "not a runner" in cx.note
    assert "not a runner" in available_models("pi", run=machine["run"]).note
    empty = available_models("opencode", run=FakeRun(opencode_models=""))
    assert empty.models == [] and "no models listed" in empty.error


def test_verify_claude_model(machine):
    from curator.setup_wizard import verify_claude_model
    ok, detail = verify_claude_model("sonnet", run=machine["run"])
    assert ok and detail == "sonnet"
    assert machine["run"].calls[-1][:4] == ["claude", "-p", "Reply with the single word OK.", "--model"]
    bad, detail = verify_claude_model("nope", run=FakeRun(claude_verify_ok=False))
    assert not bad and "not available" in detail


def test_full_run_writes_config_and_installs_chosen_hooks(machine, home):
    from curator import config, integrations
    write_skill(home / "skills", "one")
    w, lines = _wizard(machine, [
        None,                 # skills dir: keep
        "y",                  # hooks auto
        "claude,opencode",    # hosts (codex left out)
        "y",                  # install now
        "y",                  # consolidation on
        "2",                  # harness: opencode
        "2",                  # model: opencode/claude-opus-5
        "24",                 # interval hours
        None,                 # idle hours
        "14",                 # stale days
        "60",                 # archive days
        "3",                  # snapshots
        "y",                  # write
    ])
    assert w.run_wizard() == 0
    cfg = config.read_user_config()
    assert cfg["skills"]["dir"] == "~/curator-home/skills"  # not Claude Code's default tree, so it is pinned
    assert cfg["hooks"] == {"auto": True, "hosts": ["claude", "opencode"]}
    assert cfg["curator"]["consolidate"] is True and cfg["curator"]["interval_hours"] == 24 and cfg["curator"]["min_idle_hours"] == 2
    assert cfg["curator"]["stale_after_days"] == 14 and cfg["curator"]["archive_after_days"] == 60 and cfg["curator"]["backup"]["keep"] == 3
    assert cfg["auxiliary"]["curator"] == {"provider": "opencode", "model": "opencode/claude-opus-5"}
    assert integ_status(integrations) == {"claude": True, "codex": False, "opencode": True}
    text = "\n".join(lines)
    assert "1 skill(s) found" in text and "opencode/claude-opus-5" in text and "consolidation   on  via opencode / opencode/claude-opus-5" in text
    # the runner config is exactly what llm_review resolves
    from curator.llm_review import resolve_runner
    spec = resolve_runner(config.load_config())
    assert (spec.kind, spec.model) == ("opencode", "opencode/claude-opus-5")


def integ_status(integrations):
    return {h: integrations._STATUS[h]()[0] for h in ("claude", "codex", "opencode")}


def test_claude_runner_offers_aliases_and_optional_live_check(machine, home):
    from curator import config
    w, lines = _wizard(machine, [None, "n", "all", "n", "n", "1", "sonnet", "y", *([None] * 5), "y"])
    assert w.run_wizard() == 0
    cfg = config.read_user_config()
    assert cfg["auxiliary"]["curator"] == {"provider": "claude", "model": "sonnet"} and cfg["curator"]["consolidate"] is False
    assert cfg["hooks"] == {"auto": False, "hosts": []}  # every detected host == no restriction
    assert any(c[:2] == ["claude", "-p"] for c in machine["run"].calls)
    assert "ok: sonnet" in "\n".join(lines) and "  1) fable" in "\n".join(lines)


def test_failed_live_check_lets_the_user_pick_again(machine, home):
    from curator import config
    machine["run"] = FakeRun(claude_verify_ok=False)
    w, lines = _wizard(machine, [None, "n", "all", "n", "n", "1", "nope", "y", "n", "opus", *([None] * 5), "y"])
    assert w.run_wizard() == 0
    assert config.read_user_config()["auxiliary"]["curator"]["model"] == "opus"
    assert "FAILED: model not available" in "\n".join(lines)


def test_declining_the_summary_writes_nothing(machine, home):
    from curator import config
    w, _ = _wizard(machine, [None, "y", "all", "n", "n", "3", *([None] * 5), "n"])
    assert w.run_wizard() == 1
    assert config.read_user_config() == {}


def test_yes_accepts_every_default_without_reading_input(machine, home, tmp_path):
    from curator import config, integrations
    from curator.setup_wizard import Wizard
    lines = []
    w = Wizard(input_fn=lambda p: (_ for _ in ()).throw(AssertionError("prompted")), print_fn=lines.append,
               which=machine["which"], run=machine["run"], assume_defaults=True)
    assert w.run_wizard() == 0
    cfg = config.read_user_config()
    assert cfg["curator"]["consolidate"] is False and cfg["curator"]["interval_hours"] == 168
    assert cfg["auxiliary"]["curator"]["provider"] == "claude" and cfg["auxiliary"]["curator"]["model"] == "opus"  # ~/.claude's own model
    assert cfg["hooks"] == {"auto": True, "hosts": []}
    assert integ_status(integrations) == {"claude": True, "codex": True, "opencode": True}


def test_custom_skills_dir_is_recorded_tilde_relative(machine, home, tmp_path):
    from curator import config
    other = tmp_path / "work" / "skills"
    write_skill(other, "w1"); write_skill(other, "w2")
    w, lines = _wizard(machine, ["~/work/skills", "n", "all", "n", "n", "3", *([None] * 5), "y"])
    assert w.run_wizard() == 0
    assert config.read_user_config()["skills"]["dir"] == "~/work/skills"
    assert "2 skill(s) found in ~/work/skills" in "\n".join(lines)


def test_no_runner_on_path_falls_back_to_auto(home, monkeypatch, tmp_path):
    from curator import config
    from curator.setup_wizard import Answers, Wizard
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg-config"))
    lines = []
    w = Wizard(input_fn=Answers([None, "n", "y"]), print_fn=lines.append, which=lambda n: None, run=FakeRun(), assume_defaults=False)
    assert w.run_wizard() == 0
    assert "no harness found" in "\n".join(lines) and "neither claude nor opencode" in "\n".join(lines)
    assert config.read_user_config()["auxiliary"]["curator"] == {"provider": "auto", "model": ""}


def test_setup_verb_routes_and_yes_flag(home, monkeypatch, tmp_path):
    from curator import __main__ as entry
    from curator import setup_wizard
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg-config"))
    seen = {}
    monkeypatch.setattr(setup_wizard.Wizard, "run_wizard", lambda self: seen.update(defaults=self.assume_defaults) or 0)
    assert entry.main(["setup", "--yes"]) == 0 and seen == {"defaults": True}
