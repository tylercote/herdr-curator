"""Telemetry from host agents — hook receiver + per-host installers.

The curator only learns anything if the agent that loads skills tells it. Every
supported host forwards two facts through a tiny shim: *the model loaded a
skill* and *the model edited a skill*. Loading is recorded as view + use —
loading a skill to act on it IS use, and the stale timer keys off
``last_used_at``; editing is a patch.

The shims are deliberately dumb: they forward the raw tool name and arguments
and ``normalize`` decides whether a path or name belongs to a skill the curator
scans (local tree, ``create_dir``, ``external_dirs``, trusted project dirs).
Anything else is ignored, so a hook can never pollute ``.usage.json``.

Hosts:

    claude    PostToolUse hook in ~/.claude/settings.json            (Skill | Read | Edit | Write)
    codex     PostToolUse + UserPromptSubmit in ~/.codex/hooks.json  (shell / exec / apply_patch, $skill mentions)
    opencode  plugin  ~/.config/opencode/plugins/curator.ts           (tool.execute.before/after)
    pi        extension ~/.pi/agent/extensions/curator.ts             (tool_execution_start/end)

Codex has no read tool: it ``cat``s a SKILL.md through its shell tool (reported to hooks
as ``Bash``), edits through ``apply_patch``, and an explicit ``$skill`` mention loads the
skill with no tool call at all — so its receiver parses command strings, patch headers and
the submitted prompt. Codex also gates every new or modified hook behind a trust review in
its TUI (``/hooks``); the installer says so rather than bypassing it.

``curator hook <host>`` reads one JSON payload on stdin and always exits 0 —
telemetry must never break the host. ``curator hooks install|uninstall|status``
manages the host configs; entries are tagged so reinstalls are idempotent and
uninstall touches nothing else.

Host configs never point at the plugin checkout directly. They point at one
stable **launcher**, ``<state>/bin/curator-hook``, which execs the *current*
plugin root's ``bin/curator hook <host>`` and exits 0 silently if the plugin is
gone. So reinstalling the plugin (Herdr has no ``plugin update``; a fresh ``plugin install``
is the upgrade path) never leaves a stale hook behind, Codex never re-demands trust for an unchanged command, and an
uninstalled plugin never spams the host with errors.

``reconcile()`` is what the Herdr ``[[startup]]`` hook runs every session: write
the launcher, install hooks for every detected host (``hooks.auto``), and
record the harness-native skill dirs in ``<state>/registered_skill_dirs.json`` — read-only,
like ``skills.external_dirs`` (``hooks.register_skill_dirs``) — so loads of skills there are counted.
"""

from __future__ import annotations

import argparse
import io
import json
import logging
import os
import re
import shlex
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from curator import paths
from curator.fsutil import atomic_write_text

logger = logging.getLogger(__name__)

HOSTS = ("claude", "codex", "opencode", "pi")
LOAD_TOOLS = frozenset({"skill", "read", "read_file", "view_file"})
PATCH_TOOLS = frozenset({"edit", "write", "multiedit", "notebookedit", "apply_patch", "edit_file", "write_file"})
SHELL_TOOLS = frozenset({"shell", "local_shell", "shell_command", "exec_command", "bash", "container.exec"})
_PATH_KEYS = ("file_path", "filePath", "path", "notebook_path")
_NAME_KEYS = ("skill", "name")
_COMMAND_KEYS = ("command", "cmd", "commands", "script")
_PATCH_KEYS = ("patch", "input")
_PROMPT_KEYS = ("prompt", "user_prompt", "message")
_REDIRECT_RE = re.compile(r'''>{1,2}\s*([^\s;|&<>()'"`]+)''')
_PATCH_FILE_RE = re.compile(r"^\*\*\* (?:Update|Add|Delete|Move to) File: (.+?)\s*$", re.M)
_MENTION_RE = re.compile(r"(?<![\w$])\$([A-Za-z0-9][\w.-]*)")
MARK = "herdr-curator"  # tags our entries inside foreign config files
_MAX_PARENTS = 32
_TEMPLATES = Path(__file__).resolve().parent.parent / "integrations"


def plugin_root() -> Path:
    return Path(__file__).resolve().parent.parent


def curator_bin() -> Path:
    return plugin_root() / "bin" / "curator"


def launcher_path() -> Path:
    return paths.state_dir() / "bin" / "curator-hook"


def plugin_root_file() -> Path:
    return paths.state_dir() / "plugin_root"


def hook_command(host: str) -> str:
    return f'"{launcher_path()}" {host}'


def _launcher_text() -> str:
    exports = "".join(f'export {var}="{os.environ[var]}"\n' for var in ("HERDR_PLUGIN_STATE_DIR", "HERDR_PLUGIN_CONFIG_DIR")
                      if os.environ.get(var))
    return (
        "#!/bin/sh\n"
        f"# {MARK} hook launcher — managed by the plugin; host configs point here so plugin updates never change them.\n"
        "# Usage: curator-hook <host>   (JSON payload on stdin)\n"
        f"{exports}"
        "# Builtins only until python3 is known to exist: hooks inherit the host's PATH, which may be empty.\n"
        'command -v python3 >/dev/null 2>&1 || exit 0                            # no python3: stay silent\n'
        'ROOT=""\n'
        '[ -r "${0%/*}/../plugin_root" ] && IFS= read -r ROOT < "${0%/*}/../plugin_root"\n'
        '[ -n "$ROOT" ] && [ -f "$ROOT/bin/curator" ] || exit 0                  # plugin gone: stay silent\n'
        'exec python3 "$ROOT/bin/curator" hook "$@"\n'
    )


def write_launcher() -> Path:
    """Write ``<state>/plugin_root`` and the launcher; idempotent, cheap enough for every startup."""
    root_file, launcher = plugin_root_file(), launcher_path()
    launcher.parent.mkdir(parents=True, exist_ok=True)
    root = str(plugin_root())
    if not root_file.exists() or root_file.read_text(encoding="utf-8").strip() != root:
        atomic_write_text(root_file, root + "\n")
    text = _launcher_text()
    if not launcher.exists() or launcher.read_text(encoding="utf-8") != text:
        atomic_write_text(launcher, text)
    launcher.chmod(0o755)
    return launcher


# --- resolution ------------------------------------------------------------------------

def skill_index() -> Dict[Path, str]:
    """``resolved SKILL.md path -> canonical name`` over every dir ``skill_view`` scans.

    Resolved paths so a symlinked skill (``~/.claude/skills/x -> ../../.agents/skills/x``)
    is found whichever path the host reports."""
    from curator.skill_utils import EXCLUDED_SKILL_DIRS, iter_skill_index_files
    from curator.skills_tool import MAX_NAME_LENGTH, _parse_frontmatter, _read_skill_text, _skill_search_dirs
    _, dirs, _ = _skill_search_dirs()
    index: Dict[Path, str] = {}
    for d in dirs:
        for md in iter_skill_index_files(Path(d), "SKILL.md"):
            if any(part in EXCLUDED_SKILL_DIRS for part in md.parts):
                continue
            try:
                frontmatter, _ = _parse_frontmatter(_read_skill_text(md)[:4000])
                name = str(frontmatter.get("name") or md.parent.name)[:MAX_NAME_LENGTH]
            except Exception:
                name = md.parent.name
            index.setdefault(md.resolve(), name)
    return index


def _scan_roots() -> List[Path]:
    """Every directory ``skill_index`` would walk — lexical and resolved — plus the real targets of
    skills linked into the curated tree (a host may report either side of such a link)."""
    from curator.skill_utils import _local_link_targets
    from curator.skills_tool import _skill_search_dirs
    roots: List[Path] = []
    for d in _skill_search_dirs()[1]:
        for candidate in (Path(d).absolute(), Path(d).resolve()):
            if candidate not in roots:
                roots.append(candidate)
    roots.extend(t for t in _local_link_targets() if t not in roots)
    return roots


def _under_any(p: Path, roots: List[Path]) -> bool:
    return any(p == r or r in p.parents for r in roots)


def resolve_path(path: Any) -> Optional[str]:
    """Skill name for a file inside a known skill dir — SKILL.md itself or any support file.

    Cheap early-out first: hooks fire for every Read/Edit/Write the host makes, and almost all of
    them are project files, so a path under none of the scanned roots must cost a few stats, not
    a walk of every skill tree."""
    try:
        lexical = paths.expanduser(str(path)).absolute()
        p = lexical.resolve()
    except Exception:
        return None
    roots = _scan_roots()
    if not roots or not (_under_any(lexical, roots) or _under_any(p, roots)):
        return None
    index = skill_index()
    if not index:
        return None
    for d in (p, *list(p.parents)[:_MAX_PARENTS]):
        name = index.get(d / "SKILL.md")
        if name:
            return name
    return None


def resolve_name(raw: Any) -> Optional[str]:
    """A host's skill name (``x``, ``plugin:x``, ``apps/web:x``) -> known name, or None."""
    name = str(raw or "").strip().rsplit(":", 1)[-1].strip()
    if not name:
        return None
    return name if name in set(skill_index().values()) else None


def _first_known(candidates: Any, cwd: Optional[str]) -> Optional[str]:
    """First candidate path that resolves to a known skill; relative paths are cwd-relative."""
    for raw in candidates:
        p = paths.expanduser(str(raw))
        if not p.is_absolute() and cwd:
            p = Path(cwd) / p
        name = resolve_path(p)
        if name:
            return name
    return None


def _command_text(args: Dict[str, Any]) -> str:
    for key in _COMMAND_KEYS:
        value = args.get(key)
        if isinstance(value, list):
            return " ".join(str(v) for v in value)
        if isinstance(value, str):
            return value
    return ""


def _path_tokens(text: str) -> List[str]:
    try:
        tokens = shlex.split(text)
    except ValueError:
        tokens = text.split()
    out = []
    for tok in tokens:
        tok = tok.strip("'\"`;,()")
        if "/" in tok or tok.startswith("~"):
            out.append(tok)
    return out


def _shell_event(args: Dict[str, Any], cwd: Optional[str]) -> Optional[Tuple[str, str]]:
    """A shell command that reads inside a skill dir is a load; one that redirects into it is a patch."""
    text = _command_text(args)
    if not text:
        return None
    target = _first_known(_REDIRECT_RE.findall(text), cwd)
    if target:
        return "patch", target
    name = _first_known(_path_tokens(text), cwd)
    return ("load", name) if name else None


def _patch_event(args: Dict[str, Any], cwd: Optional[str]) -> Optional[Tuple[str, str]]:
    text = next((args[k] for k in _PATCH_KEYS if isinstance(args.get(k), str)), "")
    name = _first_known(_PATCH_FILE_RE.findall(text), cwd) if text else None
    if name is None:
        name = next((resolve_path(args[k]) for k in _PATH_KEYS if args.get(k)), None)
    return ("patch", name) if name else None


def _mention_event(payload: Dict[str, Any]) -> Optional[Tuple[str, str]]:
    """``$skill`` in a submitted prompt loads that skill with no tool call (Codex explicit invocation)."""
    text = next((payload[k] for k in _PROMPT_KEYS if isinstance(payload.get(k), str)), "")
    for mention in _MENTION_RE.findall(text):
        name = resolve_name(mention.rstrip(".,;:!?)"))  # "$herdr." at a sentence end
        if name:
            return "load", name
    return None


def normalize(payload: Any) -> Optional[Tuple[str, str, Optional[str]]]:
    """``(kind, skill, session)`` for a payload about a known skill, else None.

    Accepts Claude Code / Codex hook JSON (``tool_name`` / ``tool_input`` / ``session_id`` /
    ``cwd``; ``prompt`` for UserPromptSubmit) and the shim shape (``tool`` / ``args`` / ``session``)."""
    if not isinstance(payload, dict):
        return None
    session = payload.get("session") or payload.get("session_id") or payload.get("sessionID")
    session = str(session) if session else None
    cwd = payload.get("cwd") if isinstance(payload.get("cwd"), str) else None
    tool = str(payload.get("tool") or payload.get("tool_name") or "").lower()
    if not tool:
        event = _mention_event(payload)
        return (*event, session) if event else None
    args = payload.get("args") if "args" in payload else payload.get("tool_input")
    args = args if isinstance(args, dict) else {}
    event: Optional[Tuple[str, str]] = None
    if tool == "skill":
        name = next((resolve_name(args[k]) for k in _NAME_KEYS if args.get(k)), None)
        event = ("load", name) if name else None
    elif tool in SHELL_TOOLS:
        event = _shell_event(args, cwd)
    elif tool in PATCH_TOOLS:
        event = _patch_event(args, cwd)
    elif tool in LOAD_TOOLS:
        name = _first_known([args[k] for k in _PATH_KEYS if args.get(k)], cwd)
        event = ("load", name) if name else None
    return (*event, session) if event else None


def record(kind: str, skill: str, session: Optional[str] = None) -> None:
    from curator import skill_usage
    if kind == "load":
        skill_usage.bump_view(skill)
        skill_usage.bump_use(skill, session_id=session)
    elif kind == "patch":
        skill_usage.bump_patch(skill, action="edit", session_id=session)


# --- receiver ----------------------------------------------------------------------------

def hooks_log_path() -> Path:
    return paths.state_dir() / "hooks.log"


def _log(host: str, line: str) -> None:
    try:
        p = hooks_log_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        if p.exists() and p.stat().st_size > 1_000_000:
            p.unlink()
        with p.open("a", encoding="utf-8") as fh:
            fh.write(f"{datetime.now(timezone.utc).isoformat(timespec='seconds')} {host} {line}\n")
    except Exception:
        pass


def hooks_debug_path() -> Path:
    """Touch this file to log a summary of every payload received (event, tool, arg keys)."""
    return paths.state_dir() / "hooks.debug"


def _summary(payload: Any) -> str:
    if not isinstance(payload, dict):
        return f"non-object payload ({type(payload).__name__})"
    args = payload.get("args") if "args" in payload else payload.get("tool_input")
    keys = sorted(args) if isinstance(args, dict) else type(args).__name__
    prompt = next((payload[k] for k in _PROMPT_KEYS if k in payload), None)
    return (f"event={payload.get('hook_event_name') or '-'} tool={payload.get('tool') or payload.get('tool_name') or '-'} "
            f"args={keys} keys={sorted(payload)}" + (f" prompt={repr(prompt)[:240]}" if prompt is not None else ""))


def hook_main(host: str, stdin: Optional[io.TextIOBase] = None) -> int:
    """``curator hook <host>``: one payload on stdin, record if it names a known skill, exit 0."""
    try:
        raw = (stdin or sys.stdin).read()
        payload = json.loads(raw) if raw and raw.strip() else {}
        if hooks_debug_path().exists():
            _log(host, "debug " + _summary(payload))
        event = normalize(payload)
        if event:
            record(*event)
            source = (payload.get("tool") or payload.get("tool_name") or payload.get("hook_event_name") or "?") if isinstance(payload, dict) else "?"
            _log(host, f"{event[0]} {event[1]} via {source}")
    except Exception as e:  # never fail the host
        _log(host, f"error: {e}")
    return 0


# --- installers --------------------------------------------------------------------------

def claude_settings_path() -> Path:
    return Path.home() / ".claude" / "settings.json"


def codex_hooks_path() -> Path:
    return Path.home() / ".codex" / "hooks.json"


def opencode_plugin_path() -> Path:
    return paths._xdg("XDG_CONFIG_HOME", ".config") / "opencode" / "plugins" / "curator.ts"


def pi_extension_path() -> Path:
    return Path.home() / ".pi" / "agent" / "extensions" / "curator.ts"


# Codex refuses to run a new or modified hook until it is reviewed in the TUI; nothing here
# pre-trusts it, because that review is the user's safety gate.
_CODEX_TRUST_NOTE = "\n          new/changed hooks are inert until trusted: run /hooks inside Codex (automation: --dangerously-bypass-hook-trust)"

# Hosts whose hooks are JSON entries: {event: matcher-group fields}; ours get a "hooks" list appended.
_JSON_HOOKS: Dict[str, Tuple[Callable[[], Path], Dict[str, Dict[str, Any]]]] = {
    "claude": (claude_settings_path, {"PostToolUse": {"matcher": "Skill|Read|Edit|Write"}}),
    "codex": (codex_hooks_path, {"PostToolUse": {"matcher": "Bash|Edit|Write|Read|apply_patch|read_file"},  # Codex names its shell tool "Bash" to hooks
                                 "UserPromptSubmit": {}}),
}


def host_present(host: str) -> bool:
    if host == "claude":
        return (Path.home() / ".claude").is_dir() or bool(shutil.which("claude"))
    if host == "codex":
        return (Path.home() / ".codex").is_dir() or bool(shutil.which("codex"))
    if host == "opencode":
        return opencode_plugin_path().parent.parent.is_dir() or bool(shutil.which("opencode"))
    if host == "pi":
        return (Path.home() / ".pi" / "agent").is_dir() or bool(shutil.which("pi"))
    return False


def _read_json(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(data, dict):
        raise ValueError(f"{path}: top level is not an object")
    return data


def _write_host_file(path: Path, text: str) -> None:
    """Rewrite a host-owned config file in place: follow a symlink (dotfile managers link these)
    so the link survives, and keep the existing mode instead of mkstemp's 0600."""
    target = path.resolve() if path.is_symlink() else path
    target.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(target, text, preserve_mode=True, create_mode=0o644)


def _write_json(path: Path, data: Dict[str, Any]) -> None:
    _write_host_file(path, json.dumps(data, indent=2, ensure_ascii=False) + "\n")


def _is_our_command(command: Any, host: str) -> bool:
    text = str(command or "").strip()
    return text.split()[-1:] == [host] and ("curator-hook" in text or ("bin/curator" in text and " hook " in text))


def _is_our_entry(entry: Any, host: str) -> bool:
    if not isinstance(entry, dict):
        return False
    return any(isinstance(h, dict) and _is_our_command(h.get("command"), host) for h in entry.get("hooks") or [])


def _is_our_claude_entry(entry: Any) -> bool:
    return _is_our_entry(entry, "claude")


def _label(host: str) -> str:
    return f"{host + ':':<9}"


def install_json_hooks(host: str) -> str:
    write_launcher()
    path_fn, events = _JSON_HOOKS[host]
    path = path_fn()
    data = _read_json(path)
    hooks = data.setdefault("hooks", {})
    for event, fields in events.items():
        entries = hooks.setdefault(event, [])
        entries[:] = [e for e in entries if not _is_our_entry(e, host)]
        entries.append({**fields, "hooks": [{"type": "command", "command": hook_command(host)}]})
    _write_json(path, data)
    line = f"{_label(host)} installed {' + '.join(events)} hook{'s' if len(events) > 1 else ''} -> {path}"
    return line + _CODEX_TRUST_NOTE if host == "codex" else line


def uninstall_json_hooks(host: str) -> str:
    path_fn, events = _JSON_HOOKS[host]
    path = path_fn()
    data = _read_json(path)
    hooks = data.get("hooks") or {}
    removed = False
    for event in events:
        entries = hooks.get(event) or []
        kept = [e for e in entries if not _is_our_entry(e, host)]
        removed = removed or len(kept) != len(entries)
        if kept:
            hooks[event] = kept
        else:
            hooks.pop(event, None)
    if not removed:
        return f"{_label(host)} nothing installed in {path}"
    if not hooks:
        data.pop("hooks", None)
    _write_json(path, data)
    return f"{_label(host)} removed hooks from {path}"


def status_json_hooks(host: str) -> Tuple[bool, str]:
    path_fn, events = _JSON_HOOKS[host]
    path = path_fn()
    try:
        hooks = _read_json(path).get("hooks") or {}
    except Exception as e:
        return False, f"{_label(host)} cannot read {path}: {e}"
    ours = [e for event in events for e in (hooks.get(event) or []) if _is_our_entry(e, host)]
    if not ours:
        return False, f"{_label(host)} not installed ({path})"
    current = hook_command(host)
    fresh = len(ours) == len(events) and all(any(h.get("command") == current for h in e.get("hooks") or []) for e in ours)
    line = f"{_label(host)} installed{'' if fresh else ' (STALE — reinstall)'} -> {path}"
    return True, line + _CODEX_TRUST_NOTE if host == "codex" else line


def install_claude() -> str:
    return install_json_hooks("claude")


def uninstall_claude() -> str:
    return uninstall_json_hooks("claude")


def status_claude() -> Tuple[bool, str]:
    return status_json_hooks("claude")


def _render_template(host: str) -> str:
    text = (_TEMPLATES / host / "curator.ts").read_text(encoding="utf-8")
    return text.replace("__CURATOR_HOOK__", str(launcher_path()))


def _install_file(host: str, target: Path) -> str:
    write_launcher()
    if target.exists() and MARK not in target.read_text(encoding="utf-8", errors="replace"):
        raise RuntimeError(f"{target} exists and is not ours — left in place; move it aside to install")
    _write_host_file(target, _render_template(host))
    return f"{_label(host)} installed -> {target}"


def _uninstall_file(host: str, target: Path) -> str:
    if not target.exists():
        return f"{_label(host)} nothing installed ({target})"
    if MARK not in target.read_text(encoding="utf-8", errors="replace"):
        return f"{_label(host)} {target} is not ours — left in place"
    target.unlink()
    return f"{_label(host)} removed {target}"


def _status_file(host: str, target: Path) -> Tuple[bool, str]:
    if not target.exists():
        return False, f"{_label(host)} not installed ({target})"
    text = target.read_text(encoding="utf-8", errors="replace")
    if MARK not in text:
        return False, f"{_label(host)} {target} exists but is not ours"
    fresh = str(launcher_path()) in text
    return True, f"{_label(host)} installed{'' if fresh else ' (STALE path — reinstall)'} -> {target}"


_INSTALL: Dict[str, Callable[[], str]] = {
    "claude": install_claude,
    "codex": lambda: install_json_hooks("codex"),
    "opencode": lambda: _install_file("opencode", opencode_plugin_path()),
    "pi": lambda: _install_file("pi", pi_extension_path()),
}
_UNINSTALL: Dict[str, Callable[[], str]] = {
    "claude": uninstall_claude,
    "codex": lambda: uninstall_json_hooks("codex"),
    "opencode": lambda: _uninstall_file("opencode", opencode_plugin_path()),
    "pi": lambda: _uninstall_file("pi", pi_extension_path()),
}
_STATUS: Dict[str, Callable[[], Tuple[bool, str]]] = {
    "claude": status_claude,
    "codex": lambda: status_json_hooks("codex"),
    "opencode": lambda: _status_file("opencode", opencode_plugin_path()),
    "pi": lambda: _status_file("pi", pi_extension_path()),
}


# --- harness skill dirs ------------------------------------------------------------------

def harness_skill_dirs() -> List[Path]:
    """Skill trees the host agents read natively (existing dirs only, curated tree excluded)."""
    candidates = [Path.home() / ".agents" / "skills", Path.home() / ".codex" / "skills",
                  paths._xdg("XDG_CONFIG_HOME", ".config") / "opencode" / "skills", Path.home() / ".pi" / "agent" / "skills",
                  Path.home() / ".claude" / "skills"]
    try:
        curated = paths.skills_dir().resolve()
    except Exception:
        curated = None
    out: List[Path] = []
    for d in candidates:
        try:
            if d.is_dir() and d.resolve() != curated and d.resolve() not in {o.resolve() for o in out}:
                out.append(d)
        except OSError:
            continue
    return out


def register_skill_dirs() -> List[str]:
    """Record the harness skill dirs in ``<state>/registered_skill_dirs.json`` (read-only to
    curation, exactly like ``skills.external_dirs``, which the user keeps by hand). Returns what was added."""
    from curator.config import read_user_config
    from curator.skill_utils import registered_skills_dirs
    configured = [e for e in (read_user_config().get("skills") or {}).get("external_dirs") or [] if isinstance(e, str)]
    known = registered_skills_dirs()
    present = {paths.expand_path(e).resolve() for e in configured + known}
    added = [paths._display(d) for d in harness_skill_dirs() if d.resolve() not in present]
    if added:
        from curator.fsutil import atomic_json_write
        atomic_json_write(paths.registered_dirs_file(), known + added)
    return added


# --- startup reconcile -----------------------------------------------------------------------

def reconcile() -> Dict[str, Any]:
    """What ``[[startup]]`` runs each session. Never raises; returns what changed."""
    from curator.config import read_config_section
    result: Dict[str, Any] = {"installed": [], "registered": [], "failed": [], "launcher": None}
    try:
        result["launcher"] = str(write_launcher())
    except Exception as e:
        result["failed"].append(f"launcher: {e}")
        return result
    cfg = read_config_section("hooks")
    if cfg.get("auto", True):
        wanted = [h for h in (cfg.get("hosts") or []) if h in HOSTS] or list(HOSTS)
        for host in wanted:
            try:
                if not host_present(host):
                    continue
                installed, line = _STATUS[host]()
                if installed and "STALE" not in line:
                    continue
                _INSTALL[host]()
                result["installed"].append(host)
            except Exception as e:
                result["failed"].append(f"{host}: {e}")
    if cfg.get("register_skill_dirs", True):
        try:
            result["registered"] = register_skill_dirs()
        except Exception as e:
            result["failed"].append(f"skill dirs: {e}")
    return result


def reconcile_notice(result: Dict[str, Any]) -> Optional[List[str]]:
    """Notification lines when a reconcile changed something, else None."""
    lines: List[str] = []
    if result.get("installed"):
        lines.append("telemetry hooks installed: " + ", ".join(result["installed"]))
        if "codex" in result["installed"]:
            lines.append("Codex: run /hooks inside Codex once to trust the new hook")
    if result.get("registered"):
        lines.append("now counting skill loads from: " + ", ".join(result["registered"]))
    if result.get("failed"):
        lines.append("hook setup problems: " + "; ".join(result["failed"]))
    return lines or None


def hooks_main(argv: Optional[Sequence[str]] = None, out: Optional[io.TextIOBase] = None) -> int:
    """``curator hooks install|uninstall|status [host ...]`` — hosts default to every one present."""
    parser = argparse.ArgumentParser(prog="curator hooks",
                                     description="Install the telemetry hooks that let host agents report skill loads/edits.")
    parser.add_argument("verb", choices=("install", "uninstall", "status", "sync", "launcher"),
                        help="install/uninstall/status per host; sync = what Herdr startup does (launcher + auto-install + "
                             "skill dirs); launcher = write the launcher only")
    parser.add_argument("host", nargs="*", choices=(*HOSTS, "all"),
                        help="claude | codex | opencode | pi | all (default: every host detected on this machine)")
    a = parser.parse_args(list(argv) if argv is not None else None)
    write = (out or sys.stdout).write
    if a.verb == "launcher":
        write(f"launcher: {write_launcher()}\n")
        return 0
    if a.verb == "sync":
        result = reconcile()
        write(f"launcher: {result['launcher']}\n")
        for line in reconcile_notice(result) or ["nothing to do"]:
            write(line + "\n")
        for host in HOSTS:
            if host_present(host):
                write(_STATUS[host]()[1] + "\n")
        return 1 if result["failed"] else 0
    hosts: List[str] = [h for h in HOSTS if h in a.host] if a.host and "all" not in a.host else list(HOSTS)
    if not a.host or "all" in a.host:
        hosts = [h for h in HOSTS if host_present(h)]
        if not hosts:
            write("no supported host agents detected (claude, codex, opencode, pi)\n")
            return 1
    rc = 0
    for host in hosts:
        try:
            if a.verb == "status":
                _, line = _STATUS[host]()
            else:
                line = (_INSTALL if a.verb == "install" else _UNINSTALL)[host]()
        except Exception as e:
            line, rc = f"{_label(host)} failed: {e}", 1
        write(line + "\n")
    if a.verb == "status":
        launcher = launcher_path()
        ok = launcher.exists() and plugin_root_file().exists()
        write(f"launcher: {launcher}{'' if ok else '  (MISSING — run `curator hooks launcher`)'}\n")
        write(f"log:      {hooks_log_path()}\n")
    if a.verb == "uninstall" and (not a.host or "all" in a.host):
        for f in (launcher_path(), plugin_root_file()):
            if f.exists():
                f.unlink()
    return rc
