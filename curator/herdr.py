"""Herdr host integration — everything that talks to the Herdr binary or runs inside a Herdr pane.

The curator has two trigger points — session start (idle = ∞) and a
60-second background tick. Here:

* ``startup``  — the manifest ``[[startup]]`` hook. Shows the first-run /
  recent-run notices as Herdr notifications, runs one ``tick`` (fully idle,
  like a CLI start), then spawns the detached ``daemon`` — the gateway tick.
* ``daemon``   — single-instance (pidfile in ``HERDR_PLUGIN_STATE_DIR``) loop:
  every ``interval`` seconds measure idleness from ``herdr agent list`` (any
  ``working`` agent resets the clock persisted in ``activity.json``) and call
  ``maybe_run_curator``. Exits when the Herdr socket disappears.
* ``action``   — manifest ``[[actions]]`` dispatch: open the matching pane.
* ``pane``     — manifest ``[[panes]]`` entrypoints rendered inside the pane.

All Herdr calls go through ``HERDR_BIN_PATH`` (portable) and are best-effort:
without Herdr, notifications print and idleness is "not measurable" (None),
which ``maybe_run_curator`` treats as fully idle — CLI-startup semantics.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import unquote, urlparse

from curator import paths

PLUGIN_ID_DEFAULT = "curator"
NOTIFY_TITLE = "Curator"


def _plugin_id() -> str:
    return os.environ.get("HERDR_PLUGIN_ID") or PLUGIN_ID_DEFAULT


def _herdr(argv: List[str], *, timeout: Optional[float] = 20) -> Tuple[int, str, str]:
    """Run ``herdr <argv>``; raises OSError when the binary is unavailable."""
    binary = os.environ.get("HERDR_BIN_PATH") or "herdr"
    proc = subprocess.run([binary, *argv], capture_output=True, text=True, timeout=timeout)
    return proc.returncode, proc.stdout, proc.stderr


def inside_herdr() -> bool:
    return os.environ.get("HERDR_ENV") == "1"


def context() -> Dict[str, Any]:
    raw = os.environ.get("HERDR_PLUGIN_CONTEXT_JSON") or ""
    try:
        data = json.loads(raw) if raw else {}
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


# --- notifications + panes ---------------------------------------------------------

def notify(title: str, body: Optional[str] = None, sound: Optional[str] = None) -> None:
    argv = ["notification", "show", title]
    if body:
        argv += ["--body", body]
    if sound:
        argv += ["--sound", sound]
    if inside_herdr():
        try:
            _herdr(argv)
            return
        except Exception:
            pass
    print(f"{title}: {body}" if body else title)


def open_pane(entrypoint: str, *, placement: Optional[str] = None, env: Optional[Dict[str, str]] = None,
              focus: bool = True) -> int:
    argv = ["plugin", "pane", "open", "--plugin", _plugin_id(), "--entrypoint", entrypoint]
    if placement:
        argv += ["--placement", placement]
    for key, value in (env or {}).items():
        argv += ["--env", f"{key}={value}"]
    if focus:
        argv.append("--focus")
    try:
        rc, _out, err = _herdr(argv)
    except Exception as e:
        print(f"curator: could not open pane {entrypoint!r}: {e}", file=sys.stderr)
        return 1
    if rc != 0:
        print(err.strip() or f"curator: herdr exited {rc}", file=sys.stderr)
    return rc


# --- idleness ------------------------------------------------------------------------

def _activity_file() -> Path:
    return paths.state_dir() / "activity.json"


def _read_last_activity() -> Optional[datetime]:
    try:
        raw = json.loads(_activity_file().read_text(encoding="utf-8")).get("last_activity_at")
        dt = datetime.fromisoformat(raw)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def _write_last_activity(now: datetime) -> None:
    try:
        _activity_file().parent.mkdir(parents=True, exist_ok=True)
        _activity_file().write_text(json.dumps({"last_activity_at": now.isoformat()}), encoding="utf-8")
    except Exception:
        pass


def _agents() -> Optional[List[Dict[str, Any]]]:
    try:
        rc, out, _err = _herdr(["agent", "list"])
        data = json.loads(out)
    except Exception:
        return None
    if isinstance(data, dict):
        data = data.get("result", data)
        if isinstance(data, dict):
            data = data.get("agents", [])
    return [a for a in data if isinstance(a, dict)] if isinstance(data, list) else []


def idle_for_seconds() -> Optional[float]:
    """Seconds since any Herdr agent was last seen working; 0 while one works; None without Herdr."""
    agents = _agents()
    if agents is None:
        return None
    now = datetime.now(timezone.utc)
    if any(str(a.get("status") or a.get("state") or "").lower() in ("working", "blocked") for a in agents):
        _write_last_activity(now)
        return 0.0
    last = _read_last_activity()
    if last is None:
        _write_last_activity(now)
        return 0.0
    return max(0.0, (now - last).total_seconds())


# --- tick / startup / daemon ----------------------------------------------------------

def _on_summary(message: str) -> None:
    notify(NOTIFY_TITLE, body=message)


def tick(*, idle_for_seconds: Optional[float] = "measure", synchronous: bool = False) -> Optional[Dict[str, Any]]:  # type: ignore[assignment]
    """One scheduler observation: ``maybe_run_curator`` with the measured idle time. The daemon
    passes ``synchronous=True`` so a pass can never outlive the process that started it."""
    from curator.curator import maybe_run_curator
    idle = globals()["idle_for_seconds"]() if idle_for_seconds == "measure" else idle_for_seconds
    return maybe_run_curator(idle_for_seconds=idle, on_summary=_on_summary, synchronous=synchronous)


def startup(*, spawn_daemon: bool = True) -> int:
    """The Herdr ``[[startup]]`` hook: notices, hook reconcile, spawn the daemon. It never ticks
    itself — this process exits immediately, which would kill an in-flight LLM pass — so the
    daemon owns every pass and its first tick is the fully-idle session-start observation."""
    from curator import integrations, notices
    first = notices.first_run_notice_text()
    if first:
        notify(NOTIFY_TITLE, body="\n".join(first))
    recent = notices.recent_run_notice_text(mark_shown=True)
    if recent:
        notify(NOTIFY_TITLE, body="\n".join(recent))
    try:  # launcher + host hooks + skill dirs; never blocks the session
        changed = integrations.reconcile_notice(integrations.reconcile())
    except Exception as e:
        changed = [f"hook setup failed: {e}"]
    if changed:
        notify(NOTIFY_TITLE, body="\n".join(changed))
    if spawn_daemon and inside_herdr():
        _spawn_detached(["daemon", "--first-idle", "inf"])  # session start == fully idle
    elif spawn_daemon:
        print("curator: not inside Herdr (HERDR_ENV unset) — daemon not started; run `curator tick` or `curator run` by hand",
              file=sys.stderr)
    return 0


def _spawn_detached(verb_args: List[str]) -> None:
    try:
        subprocess.Popen([sys.executable, str(Path(__file__).resolve().parent.parent / "bin" / "curator"), *verb_args],
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True, close_fds=True)
    except Exception as e:
        print(f"curator: could not start daemon: {e}", file=sys.stderr)


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except Exception:
        return False


def _socket_present() -> bool:
    """The daemon's reason to live. Without a socket path we can only trust ``HERDR_ENV``; a daemon
    started from a plain shell (no Herdr at all) must not loop forever."""
    sock = os.environ.get("HERDR_SOCKET_PATH")
    return inside_herdr() if not sock else Path(sock).exists()


def daemon(interval: float = 60.0, first_idle: float = float("inf")) -> int:
    """The 60 s curator tick as a detached, single-instance loop. The first tick observes
    ``first_idle`` (∞ from ``startup``: a session start is fully idle), later ticks measure idle
    via Herdr. Every pass runs synchronously inside the loop, so a pass cannot be cut short by
    the process exiting; blocking the loop during a pass is fine as it has nothing else to do."""
    pidfile = paths.state_dir() / "daemon.pid"
    try:
        pidfile.parent.mkdir(parents=True, exist_ok=True)
        existing = int(pidfile.read_text().strip()) if pidfile.exists() else None
    except Exception:
        existing = None
    if existing and existing != os.getpid() and _pid_alive(existing):
        return 1
    pidfile.write_text(str(os.getpid()), encoding="utf-8")
    idle: Any = first_idle
    try:
        while True:
            try:
                tick(idle_for_seconds=idle, synchronous=True)
            except Exception:
                pass
            idle = "measure"
            if not _socket_present():
                break
            time.sleep(interval)
    except KeyboardInterrupt:
        pass
    finally:
        try:
            pidfile.unlink()
        except OSError:
            pass
    return 0


# --- actions + panes ----------------------------------------------------------------------

def _act_open(entrypoint: str, placement: Optional[str] = None, **env: str) -> Callable[[], int]:
    return lambda: open_pane(entrypoint, placement=placement, env=env or None)


def _act_report_link() -> int:
    url = context().get("clicked_url") or os.environ.get("HERDR_PLUGIN_CLICKED_URL") or ""
    path = unquote(urlparse(url).path) if url.startswith("file://") else url
    return open_pane("report", placement="overlay", env={"CURATOR_REPORT": path} if path else None)


ACTIONS: Dict[str, Callable[[], int]] = {
    "skills": _act_open("skills", "overlay"),
    "status": _act_open("status", "overlay"),
    "run": _act_open("run", "split"),
    "dry-run": _act_open("run", "split", CURATOR_RUN_ARGS="--dry-run"),
    "consolidate": _act_open("run", "split", CURATOR_RUN_ARGS="--consolidate"),
    "report": _act_open("report", "overlay"),
    "console": _act_open("console", "overlay"),
    "setup": _act_open("setup", "overlay"),
    "report-link": _act_report_link,
}


def action(name: str) -> int:
    fn = ACTIONS.get(name)
    if fn is None:
        print(f"curator: unknown action {name!r}; known: {', '.join(sorted(ACTIONS))}", file=sys.stderr)
        return 2
    return fn()


def _wait_for_key() -> None:
    if not sys.stdin.isatty():
        return
    try:
        input("\n[press Enter to close]")
    except (EOFError, KeyboardInterrupt):
        pass


def _pane_status() -> int:
    from curator import cli
    rc = cli.cli_main(["status"])
    _wait_for_key()
    return rc


def _pane_run() -> int:
    from curator import cli
    extra = shlex.split(os.environ.get("CURATOR_RUN_ARGS", ""))
    rc = cli.cli_main(["run", *extra])
    _wait_for_key()
    return rc


def _pane_report() -> int:
    from curator import curator
    target = os.environ.get("CURATOR_REPORT")
    if not target:
        last = curator.load_state().get("last_report_path")
        target = str(Path(last) / "REPORT.md") if last else None
    if not target or not Path(target).is_file():
        print(f"curator: no report to show{f' ({target} missing)' if target else ''}")
        _wait_for_key()
        return 1
    print(Path(target).read_text(encoding="utf-8"))
    _wait_for_key()
    return 0


_CONSOLE_MENU = [
    ("s", "skills TUI (enable/disable, browse, edit)", ["skills"]),
    ("1", "status", ["status"]), ("2", "usage", ["usage"]), ("3", "run (prune-only)", ["run"]),
    ("4", "run --dry-run", ["run", "--dry-run"]), ("5", "run --consolidate", ["run", "--consolidate"]),
    ("6", "list-unmanaged", ["list-unmanaged"]), ("7", "list-archived", ["list-archived"]),
    ("8", "ledger", ["ledger"]), ("9", "rollback --list", ["rollback", "--list"]),
    ("h", "hooks status (claude / codex / opencode / pi telemetry)", ["hooks", "status"]),
    ("H", "hooks install", ["hooks", "install"]),
]


def _pane_console() -> int:
    from curator import cli
    while True:
        print("\ncurator console — pick a number, or type any curator subcommand (e.g. `pin my-skill`); q quits")
        for key, label, _argv in _CONSOLE_MENU:
            print(f"  {key}) {label}")
        try:
            answer = input("> ").strip()
        except (EOFError, KeyboardInterrupt):
            return 0
        if answer in ("q", "quit", "exit", ""):
            return 0
        argv = next((a for k, _l, a in _CONSOLE_MENU if k == answer), None) or shlex.split(answer)
        try:
            if argv[:1] == ["skills"]:
                from curator.skills_tui import cli_main as skills_main
                skills_main(argv[1:])
                continue
            if argv[:1] == ["hooks"]:
                from curator.integrations import hooks_main
                hooks_main(argv[1:])
                continue
            cli.cli_main(argv)
        except SystemExit as e:
            print(f"curator: exited {e.code}")


def _pane_setup() -> int:
    """Install telemetry hooks for every detected host now, then show status."""
    from curator.integrations import hooks_main
    rc = hooks_main(["install"])
    print()
    hooks_main(["status"])
    _wait_for_key()
    return rc


def _pane_skills() -> int:
    from curator.skills_tui import cli_main
    return cli_main([])


PANES: Dict[str, Callable[[], int]] = {"skills": _pane_skills, "status": _pane_status, "run": _pane_run,
                                       "report": _pane_report, "console": _pane_console, "setup": _pane_setup}


def pane(entrypoint: str) -> int:
    fn = PANES.get(entrypoint)
    if fn is None:
        print(f"curator: unknown pane {entrypoint!r}; known: {', '.join(sorted(PANES))}", file=sys.stderr)
        return 2
    return fn()
