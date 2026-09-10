"""Home + derived paths.

Everything is resolved at CALL time from the environment, never cached at
import, so a process can be re-pointed (tests, multi-home tooling) with an env
var alone.

Resolution -- one rule: an explicit ``CURATOR_HOME`` is a self-contained tree;
otherwise the plugin lives where Herdr puts plugin data and curates Claude
Code's skills.

    home        CURATOR_HOME > HERDR_PLUGIN_STATE_DIR
                > ${XDG_STATE_HOME:-~/.local/state}/herdr/plugins/curator
    skills dir  CURATOR_SKILLS_DIR > config ``skills.dir``
                > <home>/skills (explicit CURATOR_HOME only) > ~/.claude/skills
    config      see ``curator.config.config_path``

Herdr hands the plugin ``HERDR_PLUGIN_STATE_DIR`` / ``HERDR_PLUGIN_CONFIG_DIR``
when it spawns a manifest command; the XDG fallbacks reconstruct the same
directories so a bare ``curator`` in any shell lands on identical paths.

Layout (the split between "inside skills/" and "under home" is deliberate and
load-bearing for rollback semantics: snapshots exclude ``.curator_backups``,
and the ledger blob store must survive a tree rollback):

    <skills>/.usage.json                 telemetry + provenance sidecar
    <skills>/.archive/<name>/            archived (recoverable) skills, flat
    <skills>/.curator_state              scheduler state
    <skills>/.curator_backups/<id>/      whole-tree tar.gz snapshots
    <skills>/.curator_ledger.jsonl       per-mutation audit ledger
    <skills>/.bundled_manifest           "name:hash" lines — bundled built-ins
    <skills>/.hub/lock.json              hub-installed skills
    <skills>/.curator_suppressed         pruned built-ins the re-seeder must skip
    <home>/.curator_backups/blobs/       content-addressed ledger blobs
    <home>/logs/curator/<stamp>/         per-run run.json + REPORT.md
    <home>/cron/jobs.json                scheduled jobs (skill refs are protected)
"""

from __future__ import annotations

import os
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


def explicit_home() -> bool:
    """True when ``CURATOR_HOME`` is set: the caller wants a self-contained tree."""
    return bool(os.environ.get("CURATOR_HOME"))


def herdr_state_dir() -> Path:
    """Where Herdr keeps this plugin's state (``HERDR_PLUGIN_STATE_DIR``, or its XDG location)."""
    raw = os.environ.get("HERDR_PLUGIN_STATE_DIR")
    return Path(raw) if raw else _xdg("XDG_STATE_HOME", ".local/state") / "herdr" / "plugins" / PLUGIN_ID


def herdr_config_dir() -> Path:
    """Where Herdr keeps this plugin's config (``HERDR_PLUGIN_CONFIG_DIR``, or its XDG location)."""
    raw = os.environ.get("HERDR_PLUGIN_CONFIG_DIR")
    return Path(raw) if raw else _xdg("XDG_CONFIG_HOME", ".config") / "herdr" / "plugins" / "config" / PLUGIN_ID


def get_home() -> Path:
    raw = os.environ.get("CURATOR_HOME")
    return expanduser(raw) if raw else herdr_state_dir()


def _display(path: Path) -> str:
    """``~``-relative form for messages."""
    try:
        return "~/" + str(path.relative_to(Path.home()))
    except ValueError:
        return str(path)


def display_home() -> str:
    return _display(get_home())


def display_skills_dir() -> str:
    return _display(skills_dir())


def expand_path(entry: str) -> Path:
    """``~`` and ``${VAR}`` expanded; a relative entry is relative to HOME."""
    p = expanduser(os.path.expandvars(str(entry)))
    return p if p.is_absolute() else get_home() / p


def default_skills_dir() -> Path:
    """The tree to curate when nothing points us at one: Claude Code's, unless the
    caller asked for a self-contained home."""
    return get_home() / "skills" if explicit_home() else Path.home() / ".claude" / "skills"


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


def usage_file() -> Path:
    return skills_dir() / ".usage.json"


def archive_dir() -> Path:
    return skills_dir() / ".archive"


def state_file() -> Path:
    return skills_dir() / ".curator_state"


def backups_dir() -> Path:
    return skills_dir() / ".curator_backups"


def ledger_path() -> Path:
    return skills_dir() / ".curator_ledger.jsonl"


def blobs_dir() -> Path:
    return get_home() / ".curator_backups" / "blobs"


def reports_root() -> Path:
    return get_home() / "logs" / "curator"


def cron_dir() -> Path:
    return get_home() / "cron"


def cron_jobs_file() -> Path:
    return cron_dir() / "jobs.json"


def plugin_state_dir() -> Path:
    """Daemon pidfile + activity clock. Herdr's explicit ``HERDR_PLUGIN_STATE_DIR`` wins;
    a self-contained home keeps them in a subdir; otherwise this is Herdr's state dir,
    which is also home."""
    if os.environ.get("HERDR_PLUGIN_STATE_DIR"):
        return herdr_state_dir()
    return get_home() / ".curator_plugin" if explicit_home() else herdr_state_dir()
