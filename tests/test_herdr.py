"""curator.herdr — the Herdr host layer (context, notifications, panes, tick/daemon) and the
manifest that wires it into Herdr. Hermes has two trigger points (CLI session start + a 60 s
gateway tick); here they are the ``[[startup]]`` hook and the detached ``curator daemon``."""

from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from conftest import ROOT


class FakeHerdr:
    """Records ``herdr`` CLI invocations and answers ``agent list`` from a canned list."""

    def __init__(self, agents=None, fail=False):
        self.calls = []
        self.agents = agents or []
        self.fail = fail

    def __call__(self, argv, *, timeout=None):
        self.calls.append(list(argv))
        if self.fail:
            raise OSError("no herdr")
        if argv[:2] == ["agent", "list"]:
            return 0, json.dumps({"result": {"agents": self.agents}}), ""
        return 0, "{}", ""


@pytest.fixture
def herdr_env(home, monkeypatch, tmp_path):
    from curator import herdr
    state = tmp_path / "state"
    state.mkdir()
    monkeypatch.setenv("HERDR_PLUGIN_STATE_DIR", str(state))
    monkeypatch.setenv("HERDR_PLUGIN_ID", "curator")
    monkeypatch.setenv("HERDR_ENV", "1")
    fake = FakeHerdr()
    monkeypatch.setattr(herdr, "_herdr", fake)
    return {"herdr": herdr, "fake": fake, "state": state}


def test_context_parses_env_json(monkeypatch):
    from curator import herdr
    monkeypatch.delenv("HERDR_PLUGIN_CONTEXT_JSON", raising=False)
    assert herdr.context() == {}
    monkeypatch.setenv("HERDR_PLUGIN_CONTEXT_JSON", json.dumps({"focused_pane_cwd": "/x", "clicked_url": "file:///r.md"}))
    assert herdr.context()["focused_pane_cwd"] == "/x"
    monkeypatch.setenv("HERDR_PLUGIN_CONTEXT_JSON", "{bad")
    assert herdr.context() == {}


def test_notify_uses_herdr_when_inside_and_prints_otherwise(herdr_env, monkeypatch, capsys):
    h, fake = herdr_env["herdr"], herdr_env["fake"]
    h.notify("Curator", body="hello", sound="done")
    assert fake.calls[-1] == ["notification", "show", "Curator", "--body", "hello", "--sound", "done"]
    monkeypatch.delenv("HERDR_ENV")
    h.notify("Curator", body="printed")
    assert "printed" in capsys.readouterr().out


def test_notify_never_raises(herdr_env, monkeypatch):
    h = herdr_env["herdr"]
    monkeypatch.setattr(h, "_herdr", FakeHerdr(fail=True))
    h.notify("x")


def test_open_pane_builds_plugin_pane_open(herdr_env):
    h, fake = herdr_env["herdr"], herdr_env["fake"]
    h.open_pane("report", placement="overlay", env={"CURATOR_REPORT": "/r"}, focus=True)
    assert fake.calls[-1] == ["plugin", "pane", "open", "--plugin", "curator", "--entrypoint", "report", "--placement", "overlay",
                              "--env", "CURATOR_REPORT=/r", "--focus"]


def test_idle_seconds_from_agent_states(herdr_env, monkeypatch):
    h, fake = herdr_env["herdr"], herdr_env["fake"]
    fake.agents = [{"name": "a", "status": "idle"}]
    first = h.idle_for_seconds()
    assert first is not None and first >= 0
    fake.agents = [{"name": "a", "status": "working"}]
    assert h.idle_for_seconds() == 0.0
    fake.agents = [{"name": "a", "status": "idle"}]
    assert h.idle_for_seconds() < 5
    # No Herdr at all -> None (caller treats as "not measurable" = fully idle, like CLI startup).
    monkeypatch.setattr(h, "_herdr", FakeHerdr(fail=True))
    assert h.idle_for_seconds() is None


def test_idle_clock_persists_in_state_dir(herdr_env):
    h, fake, state = herdr_env["herdr"], herdr_env["fake"], herdr_env["state"]
    fake.agents = [{"status": "working"}]
    h.idle_for_seconds()
    activity = json.loads((state / "activity.json").read_text())
    assert "last_activity_at" in activity
    old = (datetime.now(timezone.utc) - timedelta(hours=5)).isoformat()
    (state / "activity.json").write_text(json.dumps({"last_activity_at": old}))
    fake.agents = [{"status": "idle"}]
    assert h.idle_for_seconds() > 4 * 3600


def test_tick_gates_on_idle_and_notifies(herdr_env, monkeypatch):
    from curator import curator
    h, fake = herdr_env["herdr"], herdr_env["fake"]
    calls = []
    monkeypatch.setattr(curator, "run_curator_review", lambda **kw: calls.append(kw) or {"started_at": "x"})
    assert h.tick() is None  # first observation seeds + defers
    old = (datetime.now(timezone.utc) - timedelta(hours=24 * 8)).isoformat()
    curator.save_state({**curator.load_state(), "last_run_at": old})
    fake.agents = [{"status": "working"}]
    assert h.tick() is None and calls == []
    assert h.tick(idle_for_seconds=float("inf")) == {"started_at": "x"}
    assert calls[-1]["on_summary"] is not None
    calls[-1]["on_summary"]("curator: done")
    assert ["notification", "show", "Curator", "--body", "curator: done"] == fake.calls[-1][:5]


def test_startup_shows_first_run_notice_once(herdr_env):
    h, fake, state = herdr_env["herdr"], herdr_env["fake"], herdr_env["state"]
    h.startup(spawn_daemon=False)
    notices = [c for c in fake.calls if c[:2] == ["notification", "show"]]
    assert notices and "deferred" in notices[0][4]
    fake.calls.clear()
    h.startup(spawn_daemon=False)
    assert not [c for c in fake.calls if c[:2] == ["notification", "show"]]


def test_startup_shows_recent_run_rename_map(herdr_env):
    from curator import curator
    h, fake = herdr_env["herdr"], herdr_env["fake"]
    now = datetime.now(timezone.utc).isoformat()
    curator.save_state({**curator.load_state(), "last_run_at": now, "last_run_summary": "auto: x\narchived 1 skill(s):\n  • a → b"})
    h.startup(spawn_daemon=False)
    bodies = [c[4] for c in fake.calls if c[:2] == ["notification", "show"]]
    assert any("a → b" in b for b in bodies)


def test_daemon_pidfile_single_instance_and_tick_loop(herdr_env, monkeypatch):
    h, state = herdr_env["herdr"], herdr_env["state"]
    ticks = []
    monkeypatch.setattr(h, "tick", lambda **kw: ticks.append(1))
    stop_after = {"n": 3}

    def _sleep(seconds):
        stop_after["n"] -= 1
        if stop_after["n"] == 0:
            raise KeyboardInterrupt
    monkeypatch.setattr(h.time, "sleep", _sleep)
    h.daemon(interval=1)
    assert len(ticks) == 3 and not (state / "daemon.pid").exists()
    (state / "daemon.pid").write_text(str(os.getppid()))  # a live pid that is not us
    assert h.daemon(interval=1) == 1  # another live instance owns the pidfile
    (state / "daemon.pid").write_text("999999999")
    stop_after["n"] = 1
    assert h.daemon(interval=1) == 0  # stale pid is reclaimed


def test_daemon_exits_when_herdr_socket_disappears(herdr_env, monkeypatch, tmp_path):
    h = herdr_env["herdr"]
    sock = tmp_path / "herdr.sock"
    sock.write_text("")
    monkeypatch.setenv("HERDR_SOCKET_PATH", str(sock))
    ticks = []

    def _tick(**kw):
        ticks.append(1)
        sock.unlink()
    monkeypatch.setattr(h, "tick", _tick)
    monkeypatch.setattr(h.time, "sleep", lambda s: None)
    assert h.daemon(interval=1) == 0 and len(ticks) == 1


def test_action_dispatch_opens_expected_panes(herdr_env, monkeypatch):
    h, fake = herdr_env["herdr"], herdr_env["fake"]
    h.action("status")
    assert fake.calls[-1][:7] == ["plugin", "pane", "open", "--plugin", "curator", "--entrypoint", "status"]
    h.action("dry-run")
    assert "--entrypoint" in fake.calls[-1] and "CURATOR_RUN_ARGS=--dry-run" in fake.calls[-1]
    h.action("consolidate")
    assert "CURATOR_RUN_ARGS=--consolidate" in fake.calls[-1]
    monkeypatch.setenv("HERDR_PLUGIN_CONTEXT_JSON", json.dumps({"clicked_url": "file:///tmp/logs/curator/x/REPORT.md"}))
    h.action("report-link")
    assert "CURATOR_REPORT=/tmp/logs/curator/x/REPORT.md" in fake.calls[-1]
    assert h.action("bogus") == 2


def test_pane_status_and_report_render_without_tty(herdr_env, monkeypatch, capsys):
    from curator import curator
    h = herdr_env["herdr"]
    monkeypatch.setattr(h, "_wait_for_key", lambda: None)
    assert h.pane("status") == 0
    assert "curator: ENABLED" in capsys.readouterr().out
    report_dir = curator._reports_root() / "20260101-000000"
    report_dir.mkdir(parents=True)
    (report_dir / "REPORT.md").write_text("# Curator run — hello\n", encoding="utf-8")
    curator.save_state({**curator.load_state(), "last_report_path": str(report_dir)})
    assert h.pane("report") == 0
    assert "# Curator run — hello" in capsys.readouterr().out
    monkeypatch.setenv("CURATOR_REPORT", str(report_dir / "REPORT.md"))
    assert h.pane("report") == 0
    monkeypatch.setenv("CURATOR_REPORT", "/nonexistent/REPORT.md")
    assert h.pane("report") == 1


def test_pane_run_forwards_args_to_cli(herdr_env, monkeypatch):
    from curator import cli
    h = herdr_env["herdr"]
    seen = []
    monkeypatch.setattr(cli, "cli_main", lambda argv: seen.append(argv) or 0)
    monkeypatch.setattr(h, "_wait_for_key", lambda: None)
    monkeypatch.setenv("CURATOR_RUN_ARGS", "--dry-run --consolidate")
    assert h.pane("run") == 0
    assert seen == [["run", "--dry-run", "--consolidate"]]


def test_console_menu_dispatches_and_quits(herdr_env, monkeypatch):
    from curator import cli
    h = herdr_env["herdr"]
    seen = []
    monkeypatch.setattr(cli, "cli_main", lambda argv: seen.append(argv) or 0)
    answers = iter(["1", "pin my-skill", "q"])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(answers))
    assert h.pane("console") == 0
    assert seen == [["status"], ["pin", "my-skill"]]


# --- manifest + docs consistency ------------------------------------------------

def _manifest():
    tomllib = pytest.importorskip("tomllib")
    return tomllib.loads((ROOT / "herdr-plugin.toml").read_text(encoding="utf-8"))


def test_manifest_shape_and_entrypoints_exist():
    m = _manifest()
    assert m["id"] == "curator" and m["min_herdr_version"] == "0.8.0"
    assert set(m["platforms"]) == {"macos", "linux"}
    pane_ids = {p["id"] for p in m["panes"]}
    assert {"status", "run", "report", "console"} <= pane_ids
    action_ids = {a["id"] for a in m["actions"]}
    assert {"status", "run", "dry-run", "consolidate", "report", "console", "report-link"} <= action_ids
    for item in m["actions"] + m["panes"] + m["startup"]:
        assert item["command"][0] == "sh" and "$HERDR_PLUGIN_ROOT/bin/curator" in item["command"][2]
    assert m["startup"][0]["command"][2].endswith("startup")
    handlers = {h["id"]: h for h in m["link_handlers"]}
    assert handlers["report-link"]["action"] == "report-link"
    assert re.match(handlers["report-link"]["pattern"], "file:///Users/x/.hermes/logs/curator/20260101-000000/REPORT.md")
    assert not re.match(handlers["report-link"]["pattern"], "https://example.com/REPORT.md")


def test_manifest_actions_are_dispatchable():
    from curator import herdr
    m = _manifest()
    for a in m["actions"]:
        assert a["id"] in herdr.ACTIONS, a["id"]
    for p in m["panes"]:
        assert p["id"] in herdr.PANES, p["id"]


def test_readme_documents_every_action_keybinding():
    m = _manifest()
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    for a in m["actions"]:
        if a["id"] != "report-link":
            assert f'command = "curator.{a["id"]}"' in readme, a["id"]


def test_bin_entrypoint_routes_verbs(home):
    import subprocess
    env = {**os.environ, "CURATOR_HOME": str(home)}
    out = subprocess.run([sys.executable, str(ROOT / "bin" / "curator"), "status"], env=env, capture_output=True, text=True, timeout=60)
    assert out.returncode == 0 and "curator: ENABLED" in out.stdout
    out = subprocess.run([sys.executable, str(ROOT / "bin" / "curator"), "--help"], env=env, capture_output=True, text=True, timeout=60)
    assert out.returncode == 0 and "mcp-serve" in out.stdout and "daemon" in out.stdout
