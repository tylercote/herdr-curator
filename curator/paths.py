"""Home + derived paths (mirrors ``hermes_constants.get_hermes_home`` and the
``_skills_dir()`` / ``_archive_dir()`` / ... helpers scattered across Hermes).

Everything is resolved at CALL time from the environment, never cached at
import, so a process can be re-pointed (tests, multi-home tooling) with an env
var alone.

Resolution:

    home        CURATOR_HOME > HERMES_HOME > ~/.hermes
    skills dir  CURATOR_SKILLS_DIR > config ``skills.dir`` > <home>/skills

Layout (identical to Hermes — the split between "inside skills/" and "under
home" is deliberate and load-bearing for rollback semantics):

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


def expanduser(raw: str) -> Path:
    """``~`` via ``Path.home()`` (patchable in tests; ``os.path.expanduser`` reads $HOME directly)."""
    text = str(raw)
    if text == "~":
        return Path.home()
    if text.startswith("~/"):
        return Path.home() / text[2:]
    return Path(text).expanduser()


def get_home() -> Path:
    raw = os.environ.get("CURATOR_HOME") or os.environ.get("HERMES_HOME")
    return expanduser(raw) if raw else Path.home() / ".hermes"


def display_home() -> str:
    """``~``-relative form for messages."""
    home = get_home()
    try:
        return "~/" + str(home.relative_to(Path.home()))
    except ValueError:
        return str(home)


def expand_path(entry: str) -> Path:
    """``~`` and ``${VAR}`` expanded; a relative entry is relative to HOME."""
    p = expanduser(os.path.expandvars(str(entry)))
    return p if p.is_absolute() else get_home() / p


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
    return get_home() / "skills"


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
    """Herdr-provided state dir when running as a plugin, else ``<home>/.curator_plugin``."""
    raw = os.environ.get("HERDR_PLUGIN_STATE_DIR")
    return Path(raw) if raw else get_home() / ".curator_plugin"
