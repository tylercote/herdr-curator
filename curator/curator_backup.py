"""Curator snapshot + rollback (port of ``agent/curator_backup.py``).

Before any mutating curator pass, ``<skills>/`` is tar.gz'd under
``<tree>/snapshots/<utc-iso>/`` with a ``manifest.json``. Rollback first
snapshots the CURRENT tree (so it is itself undoable), then extracts the chosen
snapshot into place. A snapshot is purely the skills: ``.git`` is excluded and
carried across a rollback untouched, and the curator's own data lives outside
the tree, so a rollback never rewinds telemetry, the ledger or the archive.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import re
import shutil
import tarfile
from datetime import datetime, timezone
from itertools import chain, count
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from curator import paths
from curator.config import read_config_section
from curator.sizefmt import format_bytes
from curator.skill_utils import is_excluded_skill_path, rglob_following_symlinks

logger = logging.getLogger(__name__)

DEFAULT_KEEP = 5
_EXCLUDE_TOP_LEVEL = {".git"}  # never captured; kept in place across a rollback
_ID_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}Z(-\d{2})?$")
_ARCHIVE_NAME = "skills.tar.gz"
_STAGING_PREFIX = ".rollback-staging-"


def _skills_dir() -> Path:
    return paths.skills_dir()


def _backups_dir() -> Path:
    return paths.backups_dir()


def _utc_id(now: Optional[datetime] = None) -> str:
    s = (datetime.now(timezone.utc) if now is None else now).replace(microsecond=0).isoformat()
    return s.removesuffix("+00:00").replace(":", "-") + "Z"


def _load_config() -> Dict[str, Any]:
    return read_config_section("curator", "backup")


def is_enabled() -> bool:
    return bool(_load_config().get("enabled", True))


def get_keep() -> int:
    try:
        return max(1, int(_load_config().get("keep", DEFAULT_KEEP)))
    except (TypeError, ValueError):
        return DEFAULT_KEEP


def _count_skill_files(base: Path) -> int:
    try:
        return sum(1 for p in rglob_following_symlinks(base, "SKILL.md") if not is_excluded_skill_path(p))
    except OSError:
        return 0


def _write_manifest(dest: Path, reason: str, archive_path: Path, skills_counted: int) -> None:
    manifest = {"id": dest.name, "reason": reason, "created_at": datetime.now(timezone.utc).isoformat(),
                "archive": archive_path.name, "archive_bytes": archive_path.stat().st_size,
                "skill_files": skills_counted, "skills_dir": str(_skills_dir())}
    (dest / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")


def _mkdir(path: Path, what: str, *, exist_ok: bool) -> bool:
    try:
        path.mkdir(parents=True, exist_ok=exist_ok)
        return True
    except OSError as e:
        logger.debug("Failed to create %s %s: %s", what, path, e)
        return False


def snapshot_skills(reason: str = "manual", *, protect_ids: Optional[Set[str]] = None) -> Optional[Path]:
    """tar.gz snapshot + prune. None when skipped (disabled, missing dir, IO error)."""
    if not is_enabled():
        logger.debug("Curator backup disabled by config; skipping snapshot")
        return None
    skills, backups = _skills_dir(), _backups_dir()
    if not skills.exists():
        logger.debug("No skills directory — nothing to back up")
        return None
    if not _mkdir(backups, "backups dir", exist_ok=True):
        return None
    base_id = _utc_id()
    snap_id = next(i for i in chain([base_id], (f"{base_id}-{n:02d}" for n in count(1))) if not (backups / i).exists())
    dest = backups / snap_id
    if not _mkdir(dest, "snapshot dir", exist_ok=False):
        return None
    archive = dest / _ARCHIVE_NAME
    try:
        with tarfile.open(archive, "w:gz", compresslevel=6) as tf:
            for entry in sorted(skills.iterdir()):
                if entry.name not in _EXCLUDE_TOP_LEVEL:
                    tf.add(str(entry), arcname=entry.name, recursive=True,
                           filter=lambda ti: None if any(p in _EXCLUDE_TOP_LEVEL for p in Path(ti.name).parts) else ti)
        _write_manifest(dest, reason, archive, _count_skill_files(skills))
    except (OSError, tarfile.TarError) as e:
        logger.debug("Curator snapshot failed: %s", e, exc_info=True)
        shutil.rmtree(dest, ignore_errors=True)
        return None
    _prune_old(keep=get_keep(), protect=protect_ids)
    logger.info("Curator snapshot created: %s (%s)", snap_id, reason)
    return dest


def _prune_old(keep: int, protect: Optional[Set[str]] = None) -> List[str]:
    protect = protect or set()
    backups = _backups_dir()
    if not backups.exists():
        return []
    dirs = [c for c in backups.iterdir() if c.is_dir()]
    entries = sorted((c for c in dirs if _ID_RE.match(c.name)), key=lambda c: c.name, reverse=True)
    doomed = [(p, "prune") for p in entries[keep:] if p.name not in protect]
    doomed += [(p, "clean stale staging dir") for p in dirs if p.name.startswith(_STAGING_PREFIX)]
    deleted: List[str] = []
    for path, what in doomed:
        try:
            shutil.rmtree(path)
            if what == "prune":
                deleted.append(path.name)
        except OSError as e:
            logger.debug("Failed to %s %s: %s", what, path, e)
    return deleted


def _read_manifest(snap_dir: Path) -> Dict[str, Any]:
    try:
        return json.loads((snap_dir / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _is_restorable(child: Path) -> bool:
    return bool(child.is_dir() and _ID_RE.match(child.name) and (child / _ARCHIVE_NAME).exists())


def _restorable_snapshots() -> List[Path]:
    backups = _backups_dir()
    return [c for c in sorted(backups.iterdir(), reverse=True) if _is_restorable(c)] if backups.exists() else []


def list_backups() -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for child in _restorable_snapshots():
        mf = {"id": child.name, "path": str(child), **_read_manifest(child)}
        try:
            mf.setdefault("archive_bytes", (child / _ARCHIVE_NAME).stat().st_size)
        except OSError:
            mf.setdefault("archive_bytes", 0)
        out.append(mf)
    return out


def _resolve_backup(backup_id: Optional[str]) -> Optional[Path]:
    if backup_id:
        target = _backups_dir() / backup_id
        return target if _ID_RE.match(backup_id) and _is_restorable(target) else None
    return next(iter(_restorable_snapshots()), None)


def _remove_entry(entry: Path) -> None:
    if entry.is_dir() and not entry.is_symlink():
        shutil.rmtree(entry)
    elif entry.exists() or entry.is_symlink():
        entry.unlink()


def _restore_excluded_subtrees(staged: Path, skills: Path) -> None:
    for dirpath, dirnames, filenames in os.walk(staged):
        for src in [Path(dirpath) / n for n in (*dirnames, *filenames) if n in _EXCLUDE_TOP_LEVEL]:
            dest = skills / src.relative_to(staged)
            if dest.parent.is_dir() and not dest.exists():
                try:
                    shutil.move(str(src), str(dest))
                except OSError as e:
                    logger.debug("Could not restore excluded entry %s: %s", src, e)
        dirnames[:] = [d for d in dirnames if d not in _EXCLUDE_TOP_LEVEL]


def _unstage(moved: List[Tuple[Path, Path]]) -> List[str]:
    failed: List[str] = []
    for orig, dest in moved:
        try:
            _remove_entry(orig)
            shutil.move(str(dest), str(orig))
        except OSError:
            failed.append(orig.name)
    return failed


def rollback(backup_id: Optional[str] = None) -> Tuple[bool, str, Optional[Path]]:
    """Restore the skills tree from a snapshot (explicit id or newest). ``(ok, message, snapshot_path)``."""
    target = _resolve_backup(backup_id)
    if target is None:
        return (False, "no matching backup found" + (f" for id '{backup_id}'" if backup_id else "")
                + " (use `curator rollback --list` to see available snapshots)", None)
    archive = target / _ARCHIVE_NAME
    if not archive.exists():
        return (False, f"snapshot {target.name} has no skills.tar.gz — corrupted?", None)
    skills, backups = _skills_dir(), _backups_dir()
    backups.mkdir(parents=True, exist_ok=True)
    try:
        safety_snapshot = snapshot_skills(reason=f"pre-rollback to {target.name}", protect_ids={target.name})
    except Exception as e:
        return (False, f"pre-rollback safety snapshot failed: {e}", None)
    if safety_snapshot is None:
        return (False, "pre-rollback safety snapshot failed; backups may be disabled "
                "or unavailable, and current skills were not changed", None)
    staged = backups / f"{_STAGING_PREFIX}{_utc_id()}"
    try:
        staged.mkdir(parents=True, exist_ok=False)
    except OSError as e:
        return (False, f"failed to create staging dir: {e}", None)
    moved: List[Tuple[Path, Path]] = []
    try:
        for entry in list(skills.iterdir()):
            if entry.name not in _EXCLUDE_TOP_LEVEL:
                shutil.move(str(entry), str(staged / entry.name))
                moved.append((entry, staged / entry.name))
    except OSError as e:
        _unstage(moved)
        shutil.rmtree(staged, ignore_errors=True)
        return (False, f"failed to stage current skills: {e}", None)
    try:
        with tarfile.open(archive, "r:gz") as tf:
            for member in tf.getmembers():
                if member.name.startswith("/") or ".." in Path(member.name).parts:
                    raise tarfile.TarError(f"refusing to extract unsafe path: {member.name!r}")
            try:
                tf.extractall(str(skills), filter="data")  # type: ignore[call-arg]
            except TypeError:
                tf.extractall(str(skills))
    except (OSError, tarfile.TarError) as e:
        keep = _EXCLUDE_TOP_LEVEL | {orig.name for orig, _ in moved}
        for entry in [x for x in skills.iterdir() if x.name not in keep]:
            with contextlib.suppress(OSError):
                _remove_entry(entry)
        unrestored = _unstage(moved)
        if unrestored:
            return (False, f"snapshot extract failed: {e} - could not restore "
                    f"{', '.join(sorted(unrestored))}; staged copies kept at {staged}", None)
        shutil.rmtree(staged, ignore_errors=True)
        return (False, f"snapshot extract failed (state restored): {e}", None)
    _restore_excluded_subtrees(staged, skills)
    shutil.rmtree(staged, ignore_errors=True)
    logger.info("Curator rollback: restored from %s", target.name)
    return True, f"restored from snapshot {target.name}", target


def summarize_backups() -> str:
    rows = list_backups()
    if not rows:
        return "No curator snapshots yet."
    header = f"{'id':<24}  {'reason':<40}  {'skills':>6}  {'size':>8}"
    return "\n".join([header, "─" * len(header)] + [
        f"{r.get('id', '?'):<24}  {(r.get('reason', '?') or '?')[:40]:<40}  "
        f"{r.get('skill_files', 0):>6}  {format_bytes(int(r.get('archive_bytes', 0))):>8}" for r in rows])
