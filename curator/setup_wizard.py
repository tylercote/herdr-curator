"""``curator setup`` — an interactive first-run flow.

Detects the coding-agent harnesses on this machine, lets the user confirm the
skills tree, pick which hosts get telemetry hooks, choose the harness (and a
model it actually offers) that runs the automated consolidation pass, and tune
the schedule — then writes the plugin ``config.json`` and installs the hooks.

Everything that talks to the outside world (``which``, subprocesses, ``input``)
is injectable, so the whole flow is unit-testable with scripted answers.

Model discovery is per harness because each exposes it differently:

    opencode   ``opencode models [provider]``  — authoritative list
    claude     no list command; ``--model`` takes tier aliases (fable/opus/sonnet/haiku) or ids.
               Optional live check: a one-word ``claude -p`` call with that model.
    codex      ``model`` from ~/.codex/config.toml, shown for information — Codex is not a
               runner for the pass (it has no headless MCP-only mode wired here).
    pi         not a runner for the pass.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence

from curator import paths

HARNESSES = ("claude", "codex", "opencode", "pi")
RUNNERS = ("claude", "opencode")  # harnesses llm_review can drive headlessly with only the MCP toolset
CLAUDE_ALIASES = ("fable", "opus", "sonnet", "haiku")
CLAUDE_IDS = ("claude-fable-5-1", "claude-opus-5", "claude-sonnet-5", "claude-haiku-4-5-20251001")
_OPENCODE_MAX_LISTED = 25

Run = Callable[..., "subprocess.CompletedProcess[str]"]


@dataclass
class Harness:
    name: str
    path: Optional[str]
    version: str = ""

    @property
    def present(self) -> bool:
        return bool(self.path)


@dataclass
class Models:
    models: List[str] = field(default_factory=list)
    configured: str = ""     # what the harness itself is set to use today, if discoverable
    source: str = ""         # where the list came from
    note: str = ""           # caveats for the user
    error: str = ""


def _run(argv: Sequence[str], run: Run, timeout: float = 30) -> "subprocess.CompletedProcess[str]":
    return run(list(argv), capture_output=True, text=True, timeout=timeout)


def detect_harnesses(*, which: Callable[[str], Optional[str]] = shutil.which, run: Run = subprocess.run) -> Dict[str, Harness]:
    out: Dict[str, Harness] = {}
    for name in HARNESSES:
        path = which(name)
        version = ""
        if path:
            try:
                proc = _run([name, "--version"], run, timeout=15)
                version = (proc.stdout or proc.stderr).strip().splitlines()[0] if (proc.stdout or proc.stderr).strip() else ""
            except Exception:
                version = ""
        out[name] = Harness(name, path, version[:60])
    return out


def _claude_configured_model() -> str:
    try:
        data = json.loads((Path.home() / ".claude" / "settings.json").read_text(encoding="utf-8-sig"))
        return str(data.get("model") or "") if isinstance(data, dict) else ""
    except Exception:
        return ""


def _codex_configured_model() -> str:
    try:
        text = (Path.home() / ".codex" / "config.toml").read_text(encoding="utf-8")
    except OSError:
        return ""
    m = re.search(r'^\s*model\s*=\s*"([^"]+)"', text, re.M)
    return m.group(1) if m else ""


def _opencode_configured_model() -> str:
    try:
        data = json.loads((paths._xdg("XDG_CONFIG_HOME", ".config") / "opencode" / "opencode.json").read_text(encoding="utf-8"))
        return str(data.get("model") or "") if isinstance(data, dict) else ""
    except Exception:
        return ""


def available_models(harness: str, *, run: Run = subprocess.run) -> Models:
    """What *harness* can run the pass with. Never raises."""
    if harness == "opencode":
        info = Models(configured=_opencode_configured_model(), source="opencode models")
        try:
            proc = _run(["opencode", "models"], run, timeout=60)
            info.models = [ln.strip() for ln in proc.stdout.splitlines() if ln.strip() and "/" in ln and " " not in ln.strip()]
            if proc.returncode != 0 and not info.models:
                info.error = (proc.stderr or proc.stdout).strip()[:200] or f"exit {proc.returncode}"
        except Exception as e:
            info.error = str(e)
        if not info.models and not info.error:
            info.error = "no models listed — is a provider configured in OpenCode?"
        return info
    if harness == "claude":
        return Models(models=[*CLAUDE_ALIASES, *CLAUDE_IDS], configured=_claude_configured_model(), source="claude --model aliases",
                      note="Claude Code has no model-list command; an alias resolves to the latest model of that tier. "
                           "Availability depends on your Claude account/plan — the optional live check below confirms it.")
    if harness == "codex":
        return Models(configured=_codex_configured_model(), source="~/.codex/config.toml",
                      note="Codex receives telemetry hooks but is not a runner for the automated pass.")
    if harness == "pi":
        return Models(note="pi receives telemetry hooks but is not a runner for the automated pass.")
    return Models(error=f"unknown harness {harness!r}")


def verify_claude_model(model: str, *, run: Run = subprocess.run) -> tuple[bool, str]:
    """One tiny headless call so a typo or an unavailable tier fails here, not in a scheduled pass."""
    try:
        proc = _run(["claude", "-p", "Reply with the single word OK.", "--model", model, "--output-format", "json",
                     "--tools", "", "--no-session-persistence"], run, timeout=120)
    except Exception as e:
        return False, str(e)
    if proc.returncode != 0:
        return False, (proc.stderr or proc.stdout).strip()[:300] or f"exit {proc.returncode}"
    try:
        data = json.loads(proc.stdout)
        if isinstance(data, dict) and data.get("is_error"):
            return False, str(data.get("result") or "claude reported an error")[:300]
        return True, str((data.get("model") if isinstance(data, dict) else "") or model)
    except ValueError:
        return True, model


# --- prompting -----------------------------------------------------------------------------

class Answers:
    """Scripted answers for tests; ``None`` = accept the default."""

    def __init__(self, answers: Sequence[Optional[str]] = ()):
        self._answers = list(answers)

    def __call__(self, prompt: str) -> str:
        if not self._answers:
            return ""
        a = self._answers.pop(0)
        return "" if a is None else a


class Wizard:
    def __init__(self, *, input_fn: Callable[[str], str] = input, print_fn: Callable[[str], None] = print,
                 which: Callable[[str], Optional[str]] = shutil.which, run: Run = subprocess.run,
                 assume_defaults: bool = False):
        self.input_fn, self.print_fn, self.which, self.run = input_fn, print_fn, which, run
        self.assume_defaults = assume_defaults
        self.summary: List[str] = []

    # -- primitives
    def say(self, text: str = "") -> None:
        self.print_fn(text)

    def ask(self, prompt: str, default: str = "") -> str:
        if self.assume_defaults:
            return default
        try:
            raw = self.input_fn(f"{prompt} [{default}]: " if default else f"{prompt}: ").strip()
        except (EOFError, KeyboardInterrupt):
            raise SystemExit("\ncurator setup: aborted, nothing written")
        return raw or default

    def ask_int(self, prompt: str, default: int, *, minimum: int = 0) -> int:
        while True:
            raw = self.ask(prompt, str(default))
            try:
                value = int(raw)
                if value >= minimum:
                    return value
            except ValueError:
                pass
            self.say(f"  please enter a whole number >= {minimum}")
            if self.assume_defaults:
                return default

    def ask_bool(self, prompt: str, default: bool) -> bool:
        raw = self.ask(f"{prompt} (y/n)", "y" if default else "n").lower()
        return raw.startswith("y") if raw else default

    def choose(self, prompt: str, options: Sequence[str], default: str, *, allow_other: bool = True) -> str:
        """Numbered menu; the user may type a number, an option verbatim, or (if allowed) anything else."""
        for i, opt in enumerate(options, 1):
            self.say(f"  {i}) {opt}{'   (default)' if opt == default else ''}")
        if allow_other:
            self.say("  or type a value")
        while True:
            raw = self.ask(prompt, default)
            if raw.isdigit() and 1 <= int(raw) <= len(options):
                return options[int(raw) - 1]
            if raw in options or allow_other:
                return raw
            self.say("  pick a number from the list")
            if self.assume_defaults:
                return default

    # -- steps
    def step_harnesses(self) -> Dict[str, Harness]:
        self.say("curator setup\n")
        self.say("Harnesses on this machine:")
        found = detect_harnesses(which=self.which, run=self.run)
        for h in found.values():
            self.say(f"  {h.name:<9} {'found  ' + (h.version or h.path or '') if h.present else 'not found'}")
        self.say()
        return found

    def step_skills_dir(self) -> Path:
        from curator.skill_utils import iter_skill_index_files
        current = paths.skills_dir()
        count = sum(1 for _ in iter_skill_index_files(current, "SKILL.md")) if current.is_dir() else 0
        self.say(f"Skills tree to curate. The curator writes nothing into it; its own data lives in\n  {paths._display(paths.state_dir())}")
        chosen = paths.expanduser(self.ask("Skills directory", paths._display(current)))
        if chosen != current:
            count = sum(1 for _ in iter_skill_index_files(chosen, "SKILL.md")) if chosen.is_dir() else 0
        self.say(f"  {count} skill(s) found in {paths._display(chosen)}" + ("" if chosen.is_dir() else "  (directory does not exist yet)"))
        self.say()
        return chosen

    def step_hooks(self, found: Dict[str, Harness]) -> Dict[str, Any]:
        present = [h for h in HARNESSES if found[h].present]
        self.say("Telemetry hooks tell the curator which skills your agents actually load.")
        if not present:
            self.say("  no harness found — hooks can be installed later with `curator hooks install`\n")
            return {"auto": True, "hosts": [], "install_now": False}
        auto = self.ask_bool("Install/refresh hooks automatically at every Herdr session start", True)
        raw = self.ask("Hosts to hook (comma-separated, or 'all')", ",".join(present))
        hosts = present if raw.strip().lower() in ("all", "") else [h.strip() for h in raw.split(",") if h.strip() in HARNESSES]
        install_now = self.ask_bool("Install the hooks now", True)
        self.say()
        return {"auto": auto, "hosts": hosts, "install_now": install_now}

    def step_runner(self, found: Dict[str, Harness]) -> Dict[str, Any]:
        self.say("Automated pass. Every 7 days (after 2 idle hours) the curator stales/archives managed skills.")
        self.say("With consolidation ON it also forks a headless agent — skills toolset only, every edit ledgered —")
        self.say("to merge overlapping managed skills. Off by default; you can also run it by hand: curator run --consolidate")
        consolidate = self.ask_bool("Enable the LLM consolidation pass on the schedule", False)
        runners = [r for r in RUNNERS if found[r].present]
        result: Dict[str, Any] = {"consolidate": consolidate, "provider": "auto", "model": ""}
        if not runners:
            self.say("  neither claude nor opencode is on PATH; the pass will look again when it runs (provider=auto)\n")
            return result
        self.say("\nWhich harness runs the pass?")
        options = runners + ["auto (first of claude, opencode found at run time)"]
        pick = self.choose("Harness", options, options[0], allow_other=False)
        provider = pick.split()[0]
        result["provider"] = provider
        if provider == "auto":
            self.say()
            return result
        info = available_models(provider, run=self.run)
        self.say(f"\nModels available through {provider} ({info.source}):")
        if info.note:
            self.say(f"  note: {info.note}")
        if info.error:
            self.say(f"  could not list models: {info.error}")
        default_model = info.configured if info.configured in info.models or (info.configured and provider == "claude") else (info.models[0] if info.models else "")
        listed = info.models[:_OPENCODE_MAX_LISTED] if provider == "opencode" else info.models
        if provider == "opencode" and len(info.models) > len(listed):
            self.say(f"  (showing {len(listed)} of {len(info.models)}; `opencode models` prints them all)")
        model = self.choose("Model (empty = the harness's own default)", listed, default_model) if listed else self.ask("Model (empty = the harness's own default)", default_model)
        if provider == "opencode" and model and info.models and model not in info.models:
            self.say(f"  warning: {model!r} is not in `opencode models`; the pass will fail if OpenCode rejects it")
        if provider == "claude" and model and self.ask_bool("Verify this model with one tiny `claude -p` call now (uses a few tokens)", False):
            ok, detail = verify_claude_model(model, run=self.run)
            self.say(f"  {'ok: ' + detail if ok else 'FAILED: ' + detail}")
            if not ok and not self.ask_bool("Keep it anyway", False):
                model = self.ask("Model", "")
        result["model"] = model
        self.say()
        return result

    def step_schedule(self) -> Dict[str, Any]:
        from curator.config import DEFAULT_CONFIG
        d = DEFAULT_CONFIG["curator"]
        self.say("Schedule and lifecycle (Enter keeps the default):")
        out = {
            "interval_hours": self.ask_int("Run every N hours", int(d["interval_hours"]), minimum=1),
            "min_idle_hours": self.ask_int("Only after N idle hours (no Herdr agent working)", int(d["min_idle_hours"])),
            "stale_after_days": self.ask_int("Mark a managed skill stale after N unused days", int(d["stale_after_days"]), minimum=1),
        }
        out["archive_after_days"] = self.ask_int("Archive it after N unused days", max(int(d["archive_after_days"]), out["stale_after_days"]),
                                                 minimum=out["stale_after_days"])
        out["backup_keep"] = self.ask_int("Whole-tree snapshots to keep", int(d["backup"]["keep"]), minimum=1)
        self.say()
        return out

    # -- apply
    def apply(self, skills: Path, hooks: Dict[str, Any], runner: Dict[str, Any], schedule: Dict[str, Any]) -> Dict[str, Any]:
        from curator.config import config_path, update_user_config

        def _mutate(cfg: Dict[str, Any]) -> None:
            if skills != paths.default_skills_dir():
                cfg.setdefault("skills", {})["dir"] = paths._display(skills)
            else:
                (cfg.get("skills") or {}).pop("dir", None)
            cfg["hooks"] = {**(cfg.get("hooks") or {}), "auto": hooks["auto"],
                            "hosts": [] if set(hooks["hosts"]) >= {h for h in HARNESSES if self.which(h)} else hooks["hosts"]}
            cur = cfg.setdefault("curator", {})
            cur.update(consolidate=runner["consolidate"], interval_hours=schedule["interval_hours"],
                       min_idle_hours=schedule["min_idle_hours"], stale_after_days=schedule["stale_after_days"],
                       archive_after_days=schedule["archive_after_days"])
            cur["backup"] = {**(cur.get("backup") or {}), "enabled": True, "keep": schedule["backup_keep"]}
            slot = cfg.setdefault("auxiliary", {}).setdefault("curator", {})
            slot["provider"], slot["model"] = runner["provider"], runner["model"]
        written = update_user_config(_mutate)
        self.say(f"wrote {paths._display(config_path())}")
        return written

    def run_wizard(self) -> int:
        found = self.step_harnesses()
        skills = self.step_skills_dir()
        hooks = self.step_hooks(found)
        runner = self.step_runner(found)
        schedule = self.step_schedule()
        self.say("Summary:")
        self.say(f"  skills tree     {paths._display(skills)}")
        self.say(f"  hooks           {'auto, ' if hooks['auto'] else 'manual, '}hosts: {', '.join(hooks['hosts']) or 'none'}")
        self.say(f"  consolidation   {'on' if runner['consolidate'] else 'off'}  via {runner['provider']}"
                 + (f" / {runner['model']}" if runner['model'] else " / harness default"))
        self.say(f"  schedule        every {schedule['interval_hours']}h after {schedule['min_idle_hours']}h idle; "
                 f"stale {schedule['stale_after_days']}d, archive {schedule['archive_after_days']}d; keep {schedule['backup_keep']} snapshots")
        if not self.ask_bool("\nWrite this configuration", True):
            self.say("nothing written")
            return 1
        self.apply(skills, hooks, runner, schedule)
        if hooks.get("install_now") and hooks["hosts"]:
            from curator.integrations import hooks_main
            hooks_main(["install", *hooks["hosts"]], out=_Writer(self.print_fn))
        self.say("\nDone. Useful next steps:\n  curator status\n  curator run --dry-run      # preview a pass, mutates nothing\n"
                 "  curator adopt <skill>      # hand a skill to the curator (nothing is managed until you do)")
        return 0


class _Writer:
    """Minimal text sink adapter so ``hooks_main`` output goes through the wizard's printer."""

    def __init__(self, print_fn: Callable[[str], None]):
        self._print = print_fn

    def write(self, text: str) -> int:
        if text.strip():
            self._print(text.rstrip("\n"))
        return len(text)


def setup_main(argv: Optional[Sequence[str]] = None, **wizard_kwargs: Any) -> int:
    import argparse
    parser = argparse.ArgumentParser(prog="curator setup", description="Interactive setup: skills tree, telemetry hooks, "
                                     "the harness + model for the automated pass, schedule.")
    parser.add_argument("--yes", "-y", action="store_true", help="accept every default (non-interactive)")
    a = parser.parse_args(list(argv) if argv is not None else None)
    return Wizard(assume_defaults=a.yes, **wizard_kwargs).run_wizard()
