"""curator.mcp_server — the skills toolset served over MCP (stdio JSON-RPC) to the review fork."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from conftest import ROOT, write_skill


def _server(tmp_path, **kw):
    from curator.mcp_server import Server
    return Server(tool_log=tmp_path / "tool_calls.jsonl", **kw)


def _call(server, name, arguments, id=1):
    resp = server.handle({"jsonrpc": "2.0", "id": id, "method": "tools/call", "params": {"name": name, "arguments": arguments}})
    text = resp["result"]["content"][0]["text"]
    return resp, json.loads(text)


def test_initialize_and_tools_list(home, tmp_path):
    server = _server(tmp_path)
    resp = server.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}})
    assert resp["id"] == 1 and resp["result"]["serverInfo"]["name"] == "curator"
    assert "tools" in resp["result"]["capabilities"]
    assert server.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None
    assert server.handle({"jsonrpc": "2.0", "id": 2, "method": "ping"})["result"] == {}
    tools = server.handle({"jsonrpc": "2.0", "id": 3, "method": "tools/list"})["result"]["tools"]
    assert [t["name"] for t in tools] == ["skills_list", "skill_view", "skill_manage"]
    assert all("inputSchema" in t and t["inputSchema"]["type"] == "object" for t in tools)
    err = server.handle({"jsonrpc": "2.0", "id": 4, "method": "nope"})
    assert err["error"]["code"] == -32601
    err = server.handle({"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {"name": "ghost", "arguments": {}}})
    assert err["result"]["isError"] is True and "Unknown tool" in err["result"]["content"][0]["text"]


def test_tools_call_logs_every_call_hermes_shaped(home, tmp_path):
    write_skill(home / "skills", "a")
    server = _server(tmp_path)
    _, listed = _call(server, "skills_list", {})
    assert listed["success"] and [s["name"] for s in listed["skills"]] == ["a"]
    _, viewed = _call(server, "skill_view", {"name": "a"})
    assert viewed["success"]
    rows = [json.loads(l) for l in (tmp_path / "tool_calls.jsonl").read_text().splitlines()]
    assert [r["name"] for r in rows] == ["skills_list", "skill_view"]
    assert json.loads(rows[1]["arguments"]) == {"name": "a"}


def test_skill_view_bumps_telemetry_like_hermes_registration(home, tmp_path):
    from curator import skill_usage
    write_skill(home / "skills", "a")
    _call(_server(tmp_path), "skill_view", {"name": "a"})
    rec = skill_usage.get_record("a")
    assert rec["view_count"] == 1 and rec["use_count"] == 1


def test_skill_manage_flat_and_operations_shapes(home, tmp_path):
    server = _server(tmp_path, background_review=False)
    content = "---\nname: n\ndescription: d.\n---\nbody\n"
    _, r = _call(server, "skill_manage", {"action": "create", "name": "n", "content": content})
    assert r["success"] is True
    _, r = _call(server, "skill_manage", {"operations": [{"action": "write_file", "name": "n", "file_path": "references/x.md", "file_content": "x"}]})
    assert r["success"] is True and r["operations_applied"] == 1


def test_background_review_guards_are_live_in_server(home, tmp_path):
    """The server binds the background-review origin: ownership + read-before-write + fail-closed delete."""
    from curator import skill_ledger, skill_usage
    write_skill(home / "skills", "mine", body="Step 1: Do the thing.")
    server = _server(tmp_path)  # background_review=True by default
    _, r = _call(server, "skill_manage", {"action": "patch", "name": "mine", "old_string": "Do the thing.", "new_string": "x"})
    assert r["success"] is False and "not curator-managed" in r["error"]
    skill_usage.adopt_skill("mine")
    _, r = _call(server, "skill_manage", {"action": "patch", "name": "mine", "old_string": "Do the thing.", "new_string": "x"})
    assert r["success"] is False and r.get("_read_before_write_required") is True
    _call(server, "skill_view", {"name": "mine"})
    _, r = _call(server, "skill_manage", {"action": "patch", "name": "mine", "old_string": "Do the thing.", "new_string": "Do it."})
    assert r["success"] is True
    assert [e["actor"] for e in skill_ledger.list_entries("mine") if e["action"] == "patch"] == ["curator"]
    _, r = _call(server, "skill_manage", {"action": "delete", "name": "mine", "absorbed_into": ""})
    assert r["success"] is False and r.get("_fail_closed") is True
    write_skill(home / "skills", "umbrella")
    skill_usage.adopt_skill("umbrella")
    _, r = _call(server, "skill_manage", {"action": "delete", "name": "mine", "absorbed_into": "umbrella"})
    assert r["success"] is True and r["_archived"] is True
    assert (home / "skills" / ".archive" / "mine").exists()


def test_dry_run_server_refuses_mutations_but_allows_reads(home, tmp_path):
    from curator import skill_usage
    write_skill(home / "skills", "mine", body="Step 1: Do the thing.")
    skill_usage.adopt_skill("mine")
    server = _server(tmp_path, dry_run=True)
    _, viewed = _call(server, "skill_view", {"name": "mine"})
    assert viewed["success"]
    _, r = _call(server, "skill_manage", {"action": "patch", "name": "mine", "old_string": "Do the thing.", "new_string": "x"})
    assert r["success"] is False and r.get("_dry_run") is True and "DRY-RUN" in r["error"]
    _, r = _call(server, "skill_manage", {"operations": [{"action": "create", "name": "z", "content": "---\nname: z\ndescription: d.\n---\nb"}]})
    assert r["success"] is False and r.get("_dry_run") is True
    assert "Do the thing." in (home / "skills" / "mine" / "SKILL.md").read_text()
    rows = [json.loads(l) for l in (tmp_path / "tool_calls.jsonl").read_text().splitlines()]
    assert [r["name"] for r in rows] == ["skill_view", "skill_manage", "skill_manage"]  # refused calls are logged too


def test_serve_loop_over_stdio_streams(home, tmp_path):
    import io
    from curator.mcp_server import Server, serve
    write_skill(home / "skills", "a")
    requests = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
                {"jsonrpc": "2.0", "method": "notifications/initialized"},
                {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "skills_list", "arguments": {}}},
                "not json"]
    stdin = io.StringIO("\n".join(json.dumps(r) if not isinstance(r, str) else r for r in requests) + "\n")
    stdout = io.StringIO()
    serve(Server(tool_log=tmp_path / "log.jsonl"), stdin=stdin, stdout=stdout)
    lines = [json.loads(l) for l in stdout.getvalue().splitlines()]
    assert lines[0]["id"] == 1 and lines[1]["id"] == 2
    assert lines[2]["error"]["code"] == -32700


def test_real_subprocess_roundtrip(home, tmp_path):
    """Spawn the actual ``bin/curator mcp-serve`` process the agents will talk to."""
    write_skill(home / "skills", "a", body="Step 1: Do the thing.")
    from curator import skill_usage
    skill_usage.adopt_skill("a")
    env = {**os.environ, "CURATOR_HOME": str(home), "PYTHONPATH": str(ROOT)}
    env.pop("HERDR_PLUGIN_CONFIG_DIR", None)
    proc = subprocess.Popen([sys.executable, str(ROOT / "bin" / "curator"), "mcp-serve", "--run-dir", str(tmp_path)],
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, text=True)
    msgs = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "skill_view", "arguments": {"name": "a"}}},
            {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "skill_manage", "arguments": {
                "action": "patch", "name": "a", "old_string": "Do the thing.", "new_string": "Done."}}}]
    out, err = proc.communicate("\n".join(json.dumps(m) for m in msgs) + "\n", timeout=60)
    assert proc.returncode == 0, err
    responses = {r["id"]: r for r in (json.loads(l) for l in out.splitlines()) if "id" in r}
    assert [t["name"] for t in responses[2]["result"]["tools"]] == ["skills_list", "skill_view", "skill_manage"]
    assert json.loads(responses[3]["result"]["content"][0]["text"])["success"] is True
    assert json.loads(responses[4]["result"]["content"][0]["text"])["success"] is True
    assert "Done." in (home / "skills" / "a" / "SKILL.md").read_text()
    rows = [json.loads(l) for l in (tmp_path / "tool_calls.jsonl").read_text().splitlines()]
    assert [r["name"] for r in rows] == ["skill_view", "skill_manage"]
