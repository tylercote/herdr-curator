"""curator.llm_review — the consolidation fork re-homed onto headless coding agents.

Hermes forks its own ``AIAgent`` with ``enabled_toolsets=["skills"]``. Here the
same prompt goes to ``claude -p`` / ``opencode run`` (or any argv template) whose
ONLY tools are the three skills tools served by ``curator mcp-serve``. These
tests never spawn a real agent — ``spawn`` is injected.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from conftest import write_skill


def _fake_spawn(stdout: str, *, returncode: int = 0, stderr: str = "", tool_calls=None):
    calls = []

    def spawn(argv, *, env, cwd, timeout):
        calls.append({"argv": list(argv), "env": dict(env), "cwd": cwd, "timeout": timeout})
        run_dir = Path(env["CURATOR_RUN_DIR"])
        if tool_calls:
            with open(run_dir / "tool_calls.jsonl", "a", encoding="utf-8") as fh:
                for tc in tool_calls:
                    fh.write(json.dumps(tc) + "\n")
        return returncode, stdout, stderr
    spawn.calls = calls
    return spawn


# --- runner resolution -------------------------------------------------------

def test_resolve_runner_auto_detects_in_order(home, monkeypatch):
    from curator import llm_review
    monkeypatch.setattr(llm_review.shutil, "which", lambda name: "/bin/x" if name == "opencode" else None)
    spec = llm_review.resolve_runner(llm_review.load_config())
    assert spec.kind == "opencode" and spec.model == "" and spec.provider_label == "opencode"
    monkeypatch.setattr(llm_review.shutil, "which", lambda name: "/bin/x" if name == "claude" else None)
    assert llm_review.resolve_runner(llm_review.load_config()).kind == "claude"
    monkeypatch.setattr(llm_review.shutil, "which", lambda name: None)
    with pytest.raises(llm_review.RunnerUnavailable):
        llm_review.resolve_runner(llm_review.load_config())


def test_resolve_runner_honours_auxiliary_slot_and_legacy(home, set_config, monkeypatch):
    from curator import llm_review
    monkeypatch.setattr(llm_review.shutil, "which", lambda name: "/bin/x")
    set_config({"auxiliary": {"curator": {"provider": "claude", "model": "claude-sonnet-5"}}})
    spec = llm_review.resolve_runner(llm_review.load_config())
    assert (spec.kind, spec.model) == ("claude", "claude-sonnet-5")
    set_config({"auxiliary": {"curator": {"provider": "auto", "model": ""}}, "model": {"provider": "opencode", "default": "openai/gpt-5"}})
    spec = llm_review.resolve_runner(llm_review.load_config())
    assert (spec.kind, spec.model) == ("opencode", "openai/gpt-5")
    set_config({"model": {"provider": "bogus-agent", "default": "m"}})
    with pytest.raises(llm_review.RunnerUnavailable):
        llm_review.resolve_runner(llm_review.load_config())


def test_command_runner_requires_argv_template(home, set_config):
    from curator import llm_review
    set_config({"auxiliary": {"curator": {"provider": "command", "model": "m"}}})
    with pytest.raises(llm_review.RunnerUnavailable):
        llm_review.resolve_runner(llm_review.load_config())
    set_config({"auxiliary": {"curator": {"provider": "command", "model": "m", "command": ["my-agent", "--model", "{model}", "--mcp", "{mcp_config}", "{prompt}"]}}})
    spec = llm_review.resolve_runner(llm_review.load_config())
    assert spec.kind == "command" and spec.command == ["my-agent", "--model", "{model}", "--mcp", "{mcp_config}", "{prompt}"]


# --- argv construction -------------------------------------------------------

def test_build_argv_claude_restricts_tools_to_the_skills_toolset(home, tmp_path):
    from curator import llm_review
    spec = llm_review.RunnerSpec(kind="claude", model="claude-sonnet-5")
    argv = llm_review.build_argv(spec, prompt="P", mcp_config=tmp_path / "mcp.json", prompt_file=tmp_path / "p.txt", run_dir=tmp_path)
    assert argv[:2] == ["claude", "-p"]
    assert "--output-format" in argv and argv[argv.index("--output-format") + 1] == "json"
    assert argv[argv.index("--mcp-config") + 1] == str(tmp_path / "mcp.json") and "--strict-mcp-config" in argv
    assert argv[argv.index("--tools") + 1] == ""  # no built-in tools: no Bash/Edit/Write bypass of the ledger
    allowed = argv[argv.index("--allowedTools") + 1].split(",")
    assert set(allowed) == {f"mcp__{llm_review.MCP_SERVER_NAME}__{t}" for t in ("skills_list", "skill_view", "skill_manage")}
    assert argv[argv.index("--model") + 1] == "claude-sonnet-5" and "--no-session-persistence" in argv
    assert argv[argv.index("--permission-mode") + 1] == "dontAsk"
    argv = llm_review.build_argv(llm_review.RunnerSpec(kind="claude", model=""), prompt="P", mcp_config=tmp_path / "m", prompt_file=tmp_path / "p", run_dir=tmp_path)
    assert "--model" not in argv


def test_build_argv_opencode_and_config(home, tmp_path):
    from curator import llm_review
    spec = llm_review.RunnerSpec(kind="opencode", model="anthropic/claude-sonnet-5")
    argv = llm_review.build_argv(spec, prompt="P", mcp_config=tmp_path / "mcp.json", prompt_file=tmp_path / "p.txt", run_dir=tmp_path)
    assert argv[:3] == ["opencode", "run", "--format"] and argv[3] == "json"
    assert argv[argv.index("--agent") + 1] == "curator" and argv[argv.index("-m") + 1] == "anthropic/claude-sonnet-5"
    assert argv[-1] == "P"
    cfg = json.loads((tmp_path / "opencode.json").read_text())
    assert cfg["mcp"][llm_review.MCP_SERVER_NAME]["type"] == "local"
    tools = cfg["agent"]["curator"]["tools"]
    assert tools["*"] is False and tools[f"{llm_review.MCP_SERVER_NAME}_*"] is True


def test_build_argv_command_template_substitution(home, tmp_path):
    from curator import llm_review
    spec = llm_review.RunnerSpec(kind="command", model="m1", command=["agent", "--model", "{model}", "--mcp", "{mcp_config}", "--prompt-file", "{prompt_file}", "{prompt}"])
    argv = llm_review.build_argv(spec, prompt="hello", mcp_config=tmp_path / "mcp.json", prompt_file=tmp_path / "p.txt", run_dir=tmp_path)
    assert argv == ["agent", "--model", "m1", "--mcp", str(tmp_path / "mcp.json"), "--prompt-file", str(tmp_path / "p.txt"), "hello"]


def test_mcp_config_points_at_this_checkout_and_propagates_home(home, tmp_path, monkeypatch):
    from curator import llm_review
    monkeypatch.setenv("CURATOR_SKILLS_DIR", str(home / "skills"))
    cfg = llm_review.build_mcp_config(run_dir=tmp_path, dry_run=True)
    server = cfg["mcpServers"][llm_review.MCP_SERVER_NAME]
    assert Path(server["args"][0]).name == "curator" and "mcp-serve" in server["args"] and "--dry-run" in server["args"]
    assert server["args"][server["args"].index("--run-dir") + 1] == str(tmp_path)
    assert server["env"]["CURATOR_HOME"] == str(home) and server["env"]["CURATOR_SKILLS_DIR"] == str(home / "skills")


# --- output parsing ----------------------------------------------------------

def test_parse_final_claude_json_and_fallbacks(home):
    from curator import llm_review
    assert llm_review.parse_final("claude", json.dumps({"type": "result", "result": "the summary", "is_error": False})) == "the summary"
    assert llm_review.parse_final("claude", "not json at all") == "not json at all"
    assert llm_review.parse_final("claude", "") == ""


def test_parse_final_opencode_json_events(home):
    from curator import llm_review
    lines = [json.dumps({"type": "step_start"}),
             json.dumps({"type": "text", "part": {"type": "text", "text": "first part "}}),
             json.dumps({"type": "text", "part": {"type": "text", "text": "second part"}}),
             json.dumps({"type": "step_finish"})]
    assert llm_review.parse_final("opencode", "\n".join(lines)) == "first part second part"
    assert llm_review.parse_final("command", "raw stdout") == "raw stdout"


def test_read_tool_calls_truncates_arguments(home, tmp_path):
    from curator import llm_review
    (tmp_path / "tool_calls.jsonl").write_text(
        json.dumps({"name": "skill_view", "arguments": json.dumps({"name": "a"})}) + "\n"
        + json.dumps({"name": "skill_manage", "arguments": json.dumps({"action": "create", "content": "x" * 1000})}) + "\n"
        + "{bad json\n", encoding="utf-8")
    calls = llm_review.read_tool_calls(tmp_path)
    assert [c["name"] for c in calls] == ["skill_view", "skill_manage"]
    assert calls[1]["arguments"].endswith("…") and len(calls[1]["arguments"]) == 401
    assert llm_review.read_tool_calls(tmp_path / "missing") == []


# --- run_review end to end with a fake spawn ---------------------------------

def test_run_review_happy_path_returns_hermes_shaped_meta(home, set_config):
    from curator import llm_review
    set_config({"auxiliary": {"curator": {"provider": "claude", "model": "claude-sonnet-5", "timeout": 42}}})
    final = "Consolidated a into b.\n\n## Structured summary (required)\n```yaml\nconsolidations: []\nprunings: []\n```"
    spawn = _fake_spawn(json.dumps({"type": "result", "result": final}),
                        tool_calls=[{"name": "skill_view", "arguments": json.dumps({"name": "a"})}])
    meta = llm_review.run_review("PROMPT", spawn=spawn)
    assert meta["error"] is None and meta["final"] == final and meta["summary"] == final[:240]
    assert meta["model"] == "claude-sonnet-5" and meta["provider"] == "claude"
    assert meta["tool_calls"] == [{"name": "skill_view", "arguments": json.dumps({"name": "a"})}]
    call = spawn.calls[0]
    assert call["timeout"] == 42 and call["argv"][0] == "claude"
    run_dir = Path(call["env"]["CURATOR_RUN_DIR"])
    assert (run_dir / "prompt.txt").read_text() == "PROMPT" and (run_dir / "mcp.json").exists()
    assert (run_dir / "stdout.txt").exists() and run_dir.is_relative_to(home / "logs" / "curator")
    assert "--dry-run" not in json.loads((run_dir / "mcp.json").read_text())["mcpServers"][llm_review.MCP_SERVER_NAME]["args"]


def test_run_review_summary_cap_and_no_change(home, set_config):
    from curator import llm_review
    set_config({"auxiliary": {"curator": {"provider": "claude", "model": "m"}}})
    long = "x" * 300
    meta = llm_review.run_review("P", spawn=_fake_spawn(json.dumps({"type": "result", "result": long})))
    assert meta["summary"] == "x" * 240 + "…"
    meta = llm_review.run_review("P", spawn=_fake_spawn(json.dumps({"type": "result", "result": ""})))
    assert meta["final"] == "" and meta["summary"] == "no change"


def test_run_review_detects_dry_run_banner_and_serves_server_dry_run(home, set_config):
    from curator import llm_review
    from curator.curator import CURATOR_DRY_RUN_BANNER
    set_config({"auxiliary": {"curator": {"provider": "claude", "model": "m"}}})
    spawn = _fake_spawn(json.dumps({"type": "result", "result": "ok"}))
    llm_review.run_review(CURATOR_DRY_RUN_BANNER + "\n\nrest", spawn=spawn)
    run_dir = Path(spawn.calls[0]["env"]["CURATOR_RUN_DIR"])
    assert "--dry-run" in json.loads((run_dir / "mcp.json").read_text())["mcpServers"][llm_review.MCP_SERVER_NAME]["args"]


def test_run_review_reports_errors_never_raises(home, set_config, monkeypatch):
    from curator import llm_review
    monkeypatch.setattr(llm_review.shutil, "which", lambda name: None)
    meta = llm_review.run_review("P")
    assert meta["error"] and "no headless agent" in meta["error"] and meta["summary"] == meta["error"]
    set_config({"auxiliary": {"curator": {"provider": "claude", "model": "m"}}})
    meta = llm_review.run_review("P", spawn=_fake_spawn("", returncode=2, stderr="boom"))
    assert meta["error"].startswith("error: claude exited 2") and "boom" in meta["error"]

    # claude -p reports API failures as exit 1 + a JSON result: surface the message, not the raw tail.
    rate_limited = json.dumps({"type": "result", "is_error": True, "api_error_status": 429,
                               "result": "You've hit your session limit"})
    meta = llm_review.run_review("P", spawn=_fake_spawn(rate_limited, returncode=1))
    assert meta["error"] == "error: claude exited 1: You've hit your session limit (HTTP 429)"

    def _timeout(argv, *, env, cwd, timeout):
        raise llm_review.subprocess.TimeoutExpired(argv, timeout)
    meta = llm_review.run_review("P", spawn=_timeout)
    assert "timed out" in meta["error"]


def test_curator_run_llm_review_delegates(home, set_config, monkeypatch):
    from curator import curator, llm_review
    set_config({"auxiliary": {"curator": {"provider": "claude", "model": "m"}}})
    monkeypatch.setattr(llm_review, "_default_spawn", _fake_spawn(json.dumps({"type": "result", "result": "delegated"})))
    meta = curator._run_llm_review("P")
    assert meta["final"] == "delegated" and meta["provider"] == "claude"
