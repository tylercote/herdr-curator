"""State dir + skills dir + every derived path.

Everything is resolved at CALL time from the environment, never cached at
import, so a process can be re-pointed (tests, multi-home tooling) with an env
var alone.

    state       HERDR_PLUGIN_STATE_DIR > CURATOR_HOME
                > ${XDG_STATE_HOME:-~/.local/state}/herdr/plugins/curator
    skills dir  CURATOR_SKILLS_DIR > config ``skills.dir`` > ~/.claude/skills
    config      see ``curator.config.config_path``

Herdr hands the plugin ``HERDR_PLUGIN_STATE_DIR`` / ``HERDR_PLUGIN_CONFIG_DIR``
when it spawns a manifest command; the XDG fallbacks reconstruct the same
directories so a bare ``curator`` in any shell lands on identical paths.

Layout. The skills tree is the user's (or Claude Code's); the curator writes
NOTHING into it. Everything it knows about a tree lives under one per-tree
directory in the state dir, keyed by the tree's resolved path, so a snapshot
is purely "the skills as they were" and a tree rollback never touches
telemetry, the ledger or the archive:

    <skills>/                          skills only (plus the user's own .git)
    <state>/
      trees/<key>/                     one per curated tree, e.g. claude-skills-3fa2b1c9
        usage.json                     telemetry + provenance sidecar (+ usage.json.lock)
        state.json                     scheduler state
        ledger.jsonl                   per-mutation audit ledger (index)
        blobs/<sha256>                 content-addressed ledger blobs
        archive/<name>/                archived (recoverable) skills, flat
        snapshots/<utc-iso>/           whole-tree tar.gz + manifest.json
      logs/curator/<stamp>/            per-run run.json + REPORT.md
      bin/curator-hook, plugin_root    stable hook launcher
      registered_skill_dirs.json       harness skill dirs found by the startup reconcile
      daemon.pid, activity.json, hooks.log
"""

from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path

PLUGIN_ID = "curator"  # must match ``id`` in herdr-plugin.toml


def expanduser(raw: str) -> Path:
    """``~`` via ``Path.home()`` (patchable in tests; ``os.path.expanduser`` reads $HOME directly)."""
    text = str(raw)
    if text == "~":
        return Path.home()
    if text.startswith("~/"):
        return Path.home() / text[2:]
    return Path(text).expanduser()


def _xdg(var: str, default_subpath: str) -> Path:
    raw = os.environ.get(var)
    return expanduser(raw) if raw else Path.home() / default_subpath


def herdr_state_dir() -> Path:
    """Where Herdr keeps this plugin's state (``HERDR_PLUGIN_STATE_DIR``, or its XDG location)."""
    raw = os.environ.get("HERDR_PLUGIN_STATE_DIR")
    return Path(raw) if raw else _xdg("XDG_STATE_HOME", ".local/state") / "herdr" / "plugins" / PLUGIN_ID


def herdr_config_dir() -> Path:
    """Where Herdr keeps this plugin's config (``HERDR_PLUGIN_CONFIG_DIR``, or its XDG location)."""
    raw = os.environ.get("HERDR_PLUGIN_CONFIG_DIR")
    return Path(raw) if raw else _xdg("XDG_CONFIG_HOME", ".config") / "herdr" / "plugins" / "config" / PLUGIN_ID


def state_dir() -> Path:
    """The one directory the curator owns. Herdr's ``HERDR_PLUGIN_STATE_DIR`` wins when set (every
    manifest command); ``CURATOR_HOME`` is the override for a bare shell, tests and tooling; else
    the XDG location, which is where Herdr puts it anyway."""
    if os.environ.get("HERDR_PLUGIN_STATE_DIR"):
        return herdr_state_dir()
    raw = os.environ.get("CURATOR_HOME")
    return expanduser(raw) if raw else herdr_state_dir()


def _display(path: Path) -> str:
    """``~``-relative form for messages."""
    try:
        return "~/" + str(path.relative_to(Path.home()))
    except ValueError:
        return str(path)


def display_skills_dir() -> str:
    return _display(skills_dir())


def expand_path(entry: str) -> Path:
    """``~`` and ``${VAR}`` expanded; a relative entry is relative to the state dir."""
    p = expanduser(os.path.expandvars(str(entry)))
    return p if p.is_absolute() else state_dir() / p


def default_skills_dir() -> Path:
    """The tree to curate when nothing points us at one: Claude Code's."""
    return Path.home() / ".claude" / "skills"


def skills_dir() -> Path:
    raw = os.environ.get("CURATOR_SKILLS_DIR")
    if raw:
        return expanduser(raw)
    try:
        from curator.config import cfg_get, load_config_readonly
        configured = cfg_get(load_config_readonly(), "skills", "dir", default="")
    except Exception:
        configured = ""
    if isinstance(configured, str) and configured.strip():
        return expand_path(configured.strip())
    return default_skills_dir()


# --- per-tree directory -----------------------------------------------------------------

_SLUG_RE = re.compile(r"[^A-Za-z0-9_-]+")


def _slug(text: str) -> str:
    return _SLUG_RE.sub("-", text.lstrip(".")).strip("-") or "tree"


def tree_key(skills: Path) -> str:
    """Stable, readable name for one skills tree: ``<parent>-<name>-<sha1[:8] of the resolved path>``
    (``~/.claude/skills`` -> ``claude-skills-3fa2b1c9``). Keyed on the *resolved* path so a
    symlinked tree maps to the same directory whichever way it is addressed."""
    resolved = os.path.realpath(str(skills))
    digest = hashlib.sha1(resolved.encode("utf-8")).hexdigest()[:8]
    p = Path(resolved)
    return f"{_slug(p.parent.name)}-{_slug(p.name)}-{digest}"


def trees_root() -> Path:
    return state_dir() / "trees"


def tree_dir() -> Path:
    """Everything the curator knows about the current skills tree."""
    return trees_root() / tree_key(skills_dir())


def usage_file() -> Path:
    return tree_dir() / "usage.json"


def state_file() -> Path:
    return tree_dir() / "state.json"


def ledger_path() -> Path:
    return tree_dir() / "ledger.jsonl"


def blobs_dir() -> Path:
    return tree_dir() / "blobs"


def archive_dir() -> Path:
    return tree_dir() / "archive"


def backups_dir() -> Path:
    return tree_dir() / "snapshots"


def reports_root() -> Path:
    return state_dir() / "logs" / "curator"


def registered_dirs_file() -> Path:
    """Harness skill dirs the startup reconcile discovered (read-only to curation, like
    ``skills.external_dirs``) — kept here so the user's config.json is never machine-edited."""
    return state_dir() / "registered_skill_dirs.json"
