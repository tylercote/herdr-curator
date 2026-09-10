"""The consolidation fork.

The fork can ONLY call ``skills_list`` / ``skill_view`` / ``skill_manage``, so
every mutation is ledgered and there is no shell to bypass the ledger with.

Herdr has no agent runtime of its own, so the same contract is reproduced with
a headless coding agent + an MCP server:

    curator ──spawn──▶ claude -p | opencode run | <custom argv>
                          │  (built-in tools disabled; only MCP tools allowed)
                          └──MCP stdio──▶ curator mcp-serve  (skills toolset,
                                          background-review origin, tool-call log)

``auxiliary.curator.provider`` picks the runner (``claude`` | ``opencode`` |
``command`` | ``auto`` = first found on PATH); ``auxiliary.curator.model`` is
passed through. The result is an ``llm_meta`` dict:
``final`` / ``summary`` (240-char cap) / ``model`` / ``provider`` / ``tool_calls``
/ ``error``, and ``tool_calls`` comes from the server's JSONL log rather than a
transcript scrape — so the classifier in ``curator`` works unchanged.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, NamedTuple, Optional

from curator import paths
from curator.config import cfg_get, load_config

logger = logging.getLogger(__name__)

MCP_SERVER_NAME = "curator"
TOOL_NAMES = ("skills_list", "skill_view", "skill_manage")
RUNNER_KINDS = ("claude", "opencode", "command")
_AUTO_ORDER = ("claude", "opencode")
_PASSTHROUGH_ENV = ("CURATOR_HOME", "CURATOR_SKILLS_DIR", "CURATOR_CONFIG", "HERDR_PLUGIN_CONFIG_DIR",
                    "HERDR_PLUGIN_STATE_DIR", "CURATOR_PROG")


class RunnerUnavailable(RuntimeError):
    pass


class RunnerSpec(NamedTuple):
    kind: str
    model: str = ""
    command: Optional[List[str]] = None

    @property
    def provider_label(self) -> str:
        return self.kind


def _bin_path() -> Path:
    return Path(__file__).resolve().parent.parent / "bin" / "curator"


def resolve_runner(cfg: Dict[str, Any]) -> RunnerSpec:
    """Runner kind + model from ``auxiliary.curator`` (canonical), legacy ``curator.auxiliary``,
    or the main ``model`` pair; ``auto`` probes PATH for ``claude`` then ``opencode``."""
    from curator.curator import _resolve_review_runtime
    binding = _resolve_review_runtime(cfg)
    provider = (binding.provider or "auto").strip().lower()
    model = (binding.model or "").strip()
    if provider in ("", "auto"):
        for kind in _AUTO_ORDER:
            if shutil.which(kind):
                return RunnerSpec(kind=kind, model=model)
        raise RunnerUnavailable(
            "no headless agent found on PATH (looked for: " + ", ".join(_AUTO_ORDER) +
            "); set auxiliary.curator.provider to claude | opencode | command")
    if provider not in RUNNER_KINDS:
        raise RunnerUnavailable(f"unknown auxiliary.curator.provider {provider!r}; expected one of {RUNNER_KINDS} or auto")
    if provider == "command":
        command = cfg_get(cfg, "auxiliary", "curator", "command", default=None)
        if not isinstance(command, list) or not command or not all(isinstance(c, str) for c in command):
            raise RunnerUnavailable("auxiliary.curator.provider=command needs auxiliary.curator.command: [argv...] "
                                    "with {prompt} / {prompt_file} / {mcp_config} / {model} placeholders")
        return RunnerSpec(kind="command", model=model, command=list(command))
    return RunnerSpec(kind=provider, model=model)


def build_mcp_config(*, run_dir: Path, dry_run: bool) -> Dict[str, Any]:
    args = [str(_bin_path()), "mcp-serve", "--run-dir", str(run_dir)]
    if dry_run:
        args.append("--dry-run")
    env = {k: v for k, v in ((k, os.environ.get(k)) for k in _PASSTHROUGH_ENV) if v}
    # The parent has already resolved every path; pin them so the child cannot re-derive
    # them differently (an explicit CURATOR_HOME alone would re-point skills under it).
    from curator.config import config_path
    env.setdefault("CURATOR_HOME", str(paths.get_home()))
    env.setdefault("CURATOR_SKILLS_DIR", str(paths.skills_dir()))
    env.setdefault("CURATOR_CONFIG", str(config_path()))
    return {"mcpServers": {MCP_SERVER_NAME: {"command": sys.executable, "args": args, "env": env}}}


def build_opencode_config(mcp_server: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "$schema": "https://opencode.ai/config.json",
        "mcp": {MCP_SERVER_NAME: {"type": "local", "command": [mcp_server["command"], *mcp_server["args"]],
                                  "environment": mcp_server.get("env", {}), "enabled": True}},
        "agent": {"curator": {"description": "Skill curator consolidation pass (ledgered skills toolset only)",
                              "mode": "primary", "tools": {"*": False, f"{MCP_SERVER_NAME}_*": True}}},
    }


def build_argv(spec: RunnerSpec, *, prompt: str, mcp_config: Path, prompt_file: Path, run_dir: Path) -> List[str]:
    if spec.kind == "claude":
        argv = ["claude", "-p", prompt, "--output-format", "json", "--mcp-config", str(mcp_config), "--strict-mcp-config",
                "--tools", "", "--allowedTools", ",".join(f"mcp__{MCP_SERVER_NAME}__{t}" for t in TOOL_NAMES),
                "--permission-mode", "dontAsk", "--no-session-persistence"]
        if spec.model:
            argv += ["--model", spec.model]
        return argv
    if spec.kind == "opencode":
        server = json.loads(mcp_config.read_text(encoding="utf-8"))["mcpServers"][MCP_SERVER_NAME] if mcp_config.exists() \
            else build_mcp_config(run_dir=run_dir, dry_run=False)["mcpServers"][MCP_SERVER_NAME]
        (run_dir / "opencode.json").write_text(json.dumps(build_opencode_config(server), indent=2), encoding="utf-8")
        argv = ["opencode", "run", "--format", "json", "--agent", "curator"]
        if spec.model:
            argv += ["-m", spec.model]
        return argv + [prompt]
    if spec.kind == "command":
        subs = {"{prompt}": prompt, "{prompt_file}": str(prompt_file), "{mcp_config}": str(mcp_config), "{model}": spec.model}
        return [subs.get(part, part) for part in (spec.command or [])]
    raise RunnerUnavailable(f"unknown runner kind {spec.kind!r}")


def _runner_env(spec: RunnerSpec, run_dir: Path) -> Dict[str, str]:
    env = dict(os.environ)
    env["CURATOR_RUN_DIR"] = str(run_dir)
    if spec.kind == "opencode":
        env["OPENCODE_CONFIG"] = str(run_dir / "opencode.json")
    return env


def parse_final(kind: str, stdout: str) -> str:
    text = (stdout or "").strip()
    if not text:
        return ""
    if kind == "claude":
        try:
            data = json.loads(text)
        except Exception:
            return text
        if isinstance(data, dict):
            return str(data.get("result") or "").strip()
        if isinstance(data, list):
            results = [d.get("result") for d in data if isinstance(d, dict) and d.get("type") == "result"]
            return str(results[-1] or "").strip() if results else text
        return text
    if kind == "opencode":
        parts: List[str] = []
        for line in text.splitlines():
            try:
                event = json.loads(line)
            except Exception:
                continue
            if not isinstance(event, dict):
                continue
            part = event.get("part") if isinstance(event.get("part"), dict) else event
            if part.get("type") == "text" and isinstance(part.get("text"), str):
                parts.append(part["text"])
        return "".join(parts).strip() if parts else text
    return text


def agent_error_detail(kind: str, stdout: str) -> str:
    """The agent's own error message when a failed run still emitted structured output
    (``claude -p`` exits 1 with ``{"is_error": true, "result": "..."}`` on e.g. a 429), else ""."""
    text = (stdout or "").strip()
    if not text:
        return ""
    try:
        data = json.loads(text)
    except Exception:
        return ""
    if isinstance(data, list):
        data = next((d for d in reversed(data) if isinstance(d, dict) and d.get("type") == "result"), None)
    if not isinstance(data, dict):
        return ""
    if data.get("is_error") or data.get("error"):
        detail = str(data.get("result") or data.get("error") or "").strip()
        status = data.get("api_error_status")
        return f"{detail} (HTTP {status})" if detail and status else detail
    return ""


def read_tool_calls(run_dir: Path) -> List[Dict[str, Any]]:
    """``[{name, arguments}]`` from the server's JSONL log; arguments capped at 400 chars."""
    path = Path(run_dir) / "tool_calls.jsonl"
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    out: List[Dict[str, Any]] = []
    for line in lines:
        try:
            row = json.loads(line)
        except Exception:
            continue
        if not isinstance(row, dict):
            continue
        a = row.get("arguments") or ""
        out.append({"name": row.get("name") or "", "arguments": a[:400] + "…" if isinstance(a, str) and len(a) > 400 else a})
    return out


def _default_spawn(argv: List[str], *, env: Dict[str, str], cwd: str, timeout: Optional[float]):
    proc = subprocess.run(argv, env=env, cwd=cwd, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL)
    return proc.returncode, proc.stdout, proc.stderr


def _new_run_dir() -> Path:
    root = paths.reports_root() / ".runs"
    root.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    run_dir, n = root / stamp, 1
    while run_dir.exists():
        n += 1
        run_dir = root / f"{stamp}-{n}"
    run_dir.mkdir(parents=True)
    return run_dir


def _llm_meta(summary: str, error: Optional[str] = None) -> Dict[str, Any]:
    return {"final": "", "summary": summary, "model": "", "provider": "", "tool_calls": [], "error": error}


def run_review(prompt: str, *, dry_run: Optional[bool] = None, spawn: Optional[Callable] = None) -> Dict[str, Any]:
    """Run the fork. Never raises. ``dry_run`` is auto-detected from the prompt banner when None."""
    if dry_run is None:
        dry_run = prompt.lstrip().startswith("═") and "DRY-RUN" in prompt[:400]
    cfg = load_config()
    try:
        spec = resolve_runner(cfg)
    except RunnerUnavailable as e:
        meta = _llm_meta(f"error: {e}", f"error: {e}")
        return meta
    meta = _llm_meta("")
    meta["model"], meta["provider"] = spec.model, spec.provider_label
    try:
        run_dir = _new_run_dir()
        (run_dir / "prompt.txt").write_text(prompt, encoding="utf-8")
        mcp_config = run_dir / "mcp.json"
        mcp_config.write_text(json.dumps(build_mcp_config(run_dir=run_dir, dry_run=dry_run), indent=2), encoding="utf-8")
        argv = build_argv(spec, prompt=prompt, mcp_config=mcp_config, prompt_file=run_dir / "prompt.txt", run_dir=run_dir)
        timeout = cfg_get(cfg, "auxiliary", "curator", "timeout", default=600)
        try:
            timeout = float(timeout) if timeout else None
        except (TypeError, ValueError):
            timeout = 600.0
        spawner = spawn or _default_spawn
        try:
            returncode, stdout, stderr = spawner(argv, env=_runner_env(spec, run_dir), cwd=str(run_dir), timeout=timeout)
        except subprocess.TimeoutExpired:
            meta["tool_calls"] = read_tool_calls(run_dir)
            meta["error"] = meta["summary"] = f"error: {spec.kind} timed out after {timeout}s"
            return meta
        (run_dir / "stdout.txt").write_text(stdout or "", encoding="utf-8")
        (run_dir / "stderr.txt").write_text(stderr or "", encoding="utf-8")
        meta["tool_calls"] = read_tool_calls(run_dir)
        if returncode != 0:
            detail = agent_error_detail(spec.kind, stdout) or (stderr or stdout or "").strip()[-400:]
            meta["error"] = meta["summary"] = f"error: {spec.kind} exited {returncode}: {detail}"
            return meta
        final = parse_final(spec.kind, stdout)
        meta["final"] = final
        meta["summary"] = (final[:240] + "…") if len(final) > 240 else (final or "no change")
    except Exception as e:
        meta["error"] = meta["summary"] = f"error: {e}"
    return meta
