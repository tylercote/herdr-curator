"""Skill usage telemetry + provenance for the Curator (port of ``tools/skill_usage.py``).

A sidecar ``<state>/trees/<key>/usage.json`` keyed by skill name (never frontmatter —
keeps telemetry out of user-authored SKILL.md).
Counter bumps are best-effort (DEBUG-logged failures never break a tool call);
writes are atomic under a cross-process lock. Curator management is an
explicit ``created_by: agent`` marker — never inferred from location.
Lifecycle: active -> stale -> archived (moved to the tree's ``archive/``); ``pinned``
opts out of auto transitions, orthogonal to state.

Record shape::

    {"created_by": None|"agent", "use_count", "view_count",
     "last_used_at", "last_viewed_at", "patch_count", "patch_generation",
     "last_reused_patch_generation", "last_patched_at", "created_at",
     "state", "pinned", "archived_at"}
"""

from __future__ import annotations

import json
import logging
from contextlib import contextmanager, suppress
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, Iterator, List, Optional, Set, Tuple

from curator import paths
from curator.fsutil import atomic_write_text
from curator.skill_utils import is_excluded_skill_path, is_external_skill_path, rglob_following_symlinks

logger = logging.getLogger(__name__)

msvcrt = None
try:
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None
    with suppress(ImportError):
        import msvcrt


STATE_ACTIVE, STATE_STALE, STATE_ARCHIVED = "active", "stale", "archived"
_VALID_STATES = {STATE_ACTIVE, STATE_STALE, STATE_ARCHIVED}

def _skills_dir() -> Path:
    return paths.skills_dir()


def _usage_file() -> Path:
    return paths.usage_file()


def _archive_dir() -> Path:
    return paths.archive_dir()


def _flock(fd, lock: bool) -> None:
    if fcntl:
        return fcntl.flock(fd, fcntl.LOCK_EX if lock else fcntl.LOCK_UN)
    fd.seek(0)
    msvcrt.locking(fd.fileno(), msvcrt.LK_LOCK if lock else msvcrt.LK_UNLCK, 1)


@contextmanager
def _usage_file_lock():
    """Serialize usage.json read-modify-write cycles across processes."""
    lock_path = _usage_file().with_suffix(".json.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    if fcntl is None and msvcrt is None:
        yield
        return
    if msvcrt and (not lock_path.exists() or lock_path.stat().st_size == 0):
        lock_path.write_text(" ", encoding="utf-8")
    with open(lock_path, "r+" if msvcrt else "a+", encoding="utf-8") as fd:
        _flock(fd, True)
        try:
            yield
        finally:
            with suppress(OSError, IOError):
                _flock(fd, False)


def _read_lines(path: Path, fail_log: str) -> List[str]:
    if not path.exists():
        return []
    try:
        return [s for s in (line.strip() for line in path.read_text(encoding="utf-8").splitlines()) if s]
    except OSError as e:
        logger.debug(fail_log, e)
        return []


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_iso_timestamp(value: Any) -> Optional[datetime]:
    try:
        parsed = datetime.fromisoformat(str(value)) if value else None
    except (TypeError, ValueError):
        return None
    return parsed.replace(tzinfo=timezone.utc) if parsed and parsed.tzinfo is None else parsed


def latest_activity_at(record: Dict[str, Any]) -> Optional[str]:
    """Newest use/view/patch timestamp; ``created_at`` excluded so never-active skills stay distinguishable."""
    stamps = [(dt, str(raw)) for raw in (record.get(k) for k in ("last_used_at", "last_viewed_at", "last_patched_at"))
              if (dt := _parse_iso_timestamp(raw)) is not None]
    return max(stamps, key=lambda t: t[0])[1] if stamps else None


def _int_or_zero(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _non_negative_int(value: Any) -> int:
    return 0 if isinstance(value, bool) else max(0, _int_or_zero(value))


def activity_count(record: Dict[str, Any]) -> int:
    return sum(_int_or_zero(record.get(key)) for key in ("use_count", "view_count", "patch_count"))


# --- Provenance ----------------------------------------------------------------
def _iter_skill_mds(base: Path, *, local_only: bool) -> Iterator[Tuple[str, Path]]:
    for skill_md in rglob_following_symlinks(base, "SKILL.md"):
        if not (is_excluded_skill_path(skill_md) or (local_only and is_external_skill_path(skill_md))):
            yield _read_skill_name(skill_md, fallback=skill_md.parent.name), skill_md


def _scan_local_skills(keep: Callable[[str, Path, Dict[str, Any]], bool]) -> List[str]:
    if not (base := _skills_dir()).exists():
        return []
    usage = load_usage()
    return sorted({name for name, skill_md in _iter_skill_mds(base, local_only=True) if keep(name, skill_md, usage)})


def list_agent_created_skill_names() -> List[str]:
    """Curator-managed skills: local skills with a ``created_by: agent`` record."""
    return _scan_local_skills(lambda name, _md, usage: _is_curator_managed_record(usage.get(name)))


def list_archived_skill_names() -> List[str]:
    root = _archive_dir()
    return sorted({p.name for p in root.iterdir() if p.is_dir()}) if root.exists() else []


def _read_skill_name(skill_md: Path, fallback: str) -> str:
    """The frontmatter ``name:`` field of a SKILL.md (first 4000 chars), else *fallback*."""
    try:
        lines = [line.strip() for line in skill_md.read_text(encoding="utf-8", errors="replace")[:4000].split("\n")]
    except OSError:
        return fallback
    if "---" not in lines:
        return fallback
    block = lines[lines.index("---") + 1:]
    block = block[:block.index("---")] if "---" in block else block
    values = (line.split(":", 1)[1].strip().strip("\"'") for line in block if line.startswith("name:"))
    return next((v for v in values if v), fallback)


def is_agent_created(skill_name: str) -> bool:
    """Lives in the curated tree (not only in an external dir) — i.e. *could* be adopted."""
    return _find_skill_dir(skill_name) is not None or _find_external_skill_dir(skill_name) is None


def _external_read_only_message(skill_name: str) -> str:
    return f"skill '{skill_name}' lives in skills.external_dirs; external skills are read-only to the curator"


def is_curation_eligible(skill_name: str, skill_path: Optional[Path] = None) -> bool:
    """Local skills: yes. External: never."""
    if skill_path is not None and is_external_skill_path(skill_path):
        return False
    local_dir = _find_skill_dir(skill_name)
    return not is_external_skill_path(local_dir) if local_dir else _find_external_skill_dir(skill_name) is None


def _is_curator_managed_record(record: Any) -> bool:
    """``created_by == "agent"`` is a curator-management OPT-IN flag, not proof of authorship — the single source of truth."""
    return isinstance(record, dict) and record.get("created_by") == "agent"


def is_curator_managed(skill_name: str) -> bool:
    return _is_curator_managed_record(load_usage().get(skill_name))


def list_unmanaged_skill_names() -> List[str]:
    """Curation-ELIGIBLE skills without a provenance marker; only ``curator adopt`` hands them over."""
    return _scan_local_skills(
        lambda name, md, usage: not _is_curator_managed_record(usage.get(name))
        and is_curation_eligible(name, md))


def unmanaged_report() -> List[Dict[str, Any]]:
    usage = load_usage()
    return [_report_row(n, usage.get(n), has_provenance_key="created_by" in usage.get(n, {}), has_record=n in usage)
            for n in list_unmanaged_skill_names()]


def adopt_skill(skill_name: str) -> Tuple[bool, str]:
    """User-declared handover: writes ``created_by: agent`` (inactivity clock NOT reset)."""
    if not skill_name:
        return False, "no skill name given"
    skill_dir = _find_skill_dir(skill_name)
    if skill_dir is None:
        if _find_external_skill_dir(skill_name) is not None:
            return False, f"'{skill_name}' lives in skills.external_dirs and is read-only to the curator"
        return False, f"skill '{skill_name}' not found"
    if is_external_skill_path(skill_dir):
        return False, _external_read_only_message(skill_name)
    if is_curator_managed(skill_name):
        return True, f"'{skill_name}' is already curator-managed"
    mark_agent_created(skill_name)
    if is_curator_managed(skill_name):
        return True, f"adopted '{skill_name}' into curator management"
    return False, f"could not mark '{skill_name}' as curator-managed"


def unadopt_skill(skill_name: str) -> Tuple[bool, str]:
    """Reverse of :func:`adopt_skill`: clears ``created_by`` so the skill reads as ``user`` again.
    Telemetry counters, pin and lifecycle state are left alone."""
    if not skill_name:
        return False, "no skill name given"
    skill_dir = _find_skill_dir(skill_name)
    if skill_dir is None:
        if _find_external_skill_dir(skill_name) is not None:
            return False, f"'{skill_name}' lives in skills.external_dirs and was never curator-managed"
        return False, f"skill '{skill_name}' not found"
    if is_external_skill_path(skill_dir):
        return False, _external_read_only_message(skill_name)
    if not is_curator_managed(skill_name):
        return True, f"'{skill_name}' is not curator-managed"
    _set_field(skill_name, "created_by", None)
    if not is_curator_managed(skill_name):
        return True, f"released '{skill_name}' from curator management (now a user skill; counters kept)"
    return False, f"could not clear the curator-managed marker on '{skill_name}'"


# --- Sidecar I/O ---------------------------------------------------------------
def _empty_record() -> Dict[str, Any]:
    return {"created_by": None, "use_count": 0, "view_count": 0, "last_used_at": None, "last_viewed_at": None,
            "patch_count": 0, "patch_generation": 0, "last_reused_patch_generation": 0, "last_patched_at": None,
            "created_at": _now_iso(), "state": STATE_ACTIVE, "pinned": False, "archived_at": None}


def _backfilled(rec: Any) -> Dict[str, Any]:
    if not isinstance(rec, dict):
        return _empty_record()
    return {**rec, **{k: v for k, v in _empty_record().items() if k not in rec}}


def _report_row(name: str, raw: Any, **extra: Any) -> Dict[str, Any]:
    row = {"name": name, **_backfilled(raw), **extra}
    row.update(last_activity_at=latest_activity_at(row), activity_count=activity_count(row))
    return row


def load_usage() -> Dict[str, Dict[str, Any]]:
    path = _usage_file()
    try:
        data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except (OSError, json.JSONDecodeError) as e:
        logger.debug("Failed to read %s: %s", path, e)
        return {}
    return {str(k): v for k, v in data.items() if isinstance(v, dict)} if isinstance(data, dict) else {}


def save_usage(data: Dict[str, Dict[str, Any]]) -> bool:
    path = _usage_file()
    try:
        atomic_write_text(path, json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False), tmp_prefix=".usage_")
        return True
    except Exception as e:
        logger.debug("Failed to write %s: %s", path, e, exc_info=True)
        return False


def get_record(skill_name: str) -> Dict[str, Any]:
    return _backfilled(load_usage().get(skill_name))


def _locked_update(skill_name: str, op: Callable[[Dict[str, Dict[str, Any]]], Tuple[Any, bool]], fail_log: str,
                   guard: Optional[Callable[[], bool]] = None) -> Any:
    try:
        if guard is not None and not guard():
            return None
        with _usage_file_lock():
            data = load_usage()
            result, dirty = op(data)
            return None if dirty and not save_usage(data) else result
    except Exception as e:
        logger.debug(fail_log, skill_name, e, exc_info=True)
        return None


def seed_record_if_missing(skill_name: str) -> None:
    """Baseline record for a curation-eligible skill so its inactivity clock starts at first sight."""
    if skill_name and is_curation_eligible(skill_name):
        def _seed(data):
            return None, skill_name not in data and data.setdefault(skill_name, _empty_record()) is not None
        _locked_update(skill_name, _seed, "skill_usage.seed_record_if_missing(%s) failed: %s")


def _mutate(skill_name: str, mutator, *, require_curation_eligible: bool = False) -> Any:
    if not skill_name:
        return None
    return _locked_update(skill_name, lambda data: (mutator(data.setdefault(skill_name, _empty_record())), True),
                          "skill_usage._mutate(%s) failed: %s",
                          (lambda: is_curation_eligible(skill_name)) if require_curation_eligible else None)


def _set_field(skill_name: str, key: str, value: Any) -> bool:
    return bool(_mutate(skill_name, lambda rec: rec.update({key: value}) or True, require_curation_eligible=True))


def _bump(rec: Dict[str, Any], count_key: str, ts_key: str) -> None:
    rec[count_key] = _non_negative_int(rec.get(count_key)) + 1
    rec[ts_key] = _now_iso()


def telemetry_provenance(skill_name: str, record: Optional[Dict[str, Any]] = None) -> str:
    """Label for lifecycle events: agent_created | external | local | unknown."""
    if isinstance(record, dict) and record.get("created_by") == "agent":
        return "agent_created"
    if _find_external_skill_dir(skill_name) is not None:
        return "external"
    return "local" if _find_skill_dir(skill_name) is not None or isinstance(record, dict) else "unknown"


def _emit_skill_lifecycle(skill_name: str, action: str, *, record: Optional[Dict[str, Any]] = None,
                          task_id: Optional[str] = None, session_id: Optional[str] = None) -> None:
    facts = record or {}
    try:
        from curator.lifecycle import has_hook, invoke_hook
        if has_hook("on_skill_lifecycle"):
            invoke_hook("on_skill_lifecycle", action=action, skill_name=skill_name,
                        provenance=telemetry_provenance(skill_name, record), task_id=task_id or "",
                        session_id=session_id or "", use_count=facts.get("use_count"), reused=facts.get("reused"),
                        reuse_after_patch=facts.get("reuse_after_patch"))
    except Exception:
        logger.debug("skill_usage lifecycle hook failed for %s/%s", skill_name, action, exc_info=True)


def _mutate_and_emit(skill_name: str, action: str, mutator: Callable[[Dict[str, Any]], Dict[str, Any]],
                     **hook_kwargs: Any) -> None:
    if isinstance(facts := _mutate(skill_name, mutator), dict):
        _emit_skill_lifecycle(skill_name, action, record=facts, **hook_kwargs)


# --- Counter bumps — telemetry for ALL skills regardless of provenance -----------
def bump_view(skill_name: str) -> None:
    _mutate(skill_name, lambda rec: _bump(rec, "view_count", "last_viewed_at"))


def bump_use(skill_name: str, *, task_id: Optional[str] = None, session_id: Optional[str] = None) -> None:
    def _apply(rec: Dict[str, Any]) -> Dict[str, Any]:
        uses = _non_negative_int(rec.get("use_count"))
        gen = _non_negative_int(rec.get("patch_generation"))
        last_reused = min(_non_negative_int(rec.get("last_reused_patch_generation")), gen)
        reuse_after_patch = uses > 0 and gen > last_reused
        rec.update(use_count=uses + 1, last_used_at=_now_iso(), patch_generation=gen,
                   last_reused_patch_generation=gen if reuse_after_patch else last_reused)
        return {"created_by": rec.get("created_by"), "use_count": uses + 1, "reused": uses > 0,
                "reuse_after_patch": reuse_after_patch}
    _mutate_and_emit(skill_name, "loaded", _apply, task_id=task_id, session_id=session_id)


def bump_patch(skill_name: str, *, action: str = "patch", task_id: Optional[str] = None,
               session_id: Optional[str] = None) -> None:
    def _apply(rec: Dict[str, Any]) -> Dict[str, Any]:
        _bump(rec, "patch_count", "last_patched_at")
        rec["patch_generation"] = _non_negative_int(rec.get("patch_generation")) + 1
        return {"created_by": rec.get("created_by")}
    _mutate_and_emit(skill_name, "patched" if action == "patch" else "edited", _apply, task_id=task_id,
                     session_id=session_id)


def record_created(skill_name: str, *, agent_created: bool, task_id: Optional[str] = None,
                   session_id: Optional[str] = None) -> None:
    def _apply(rec: Dict[str, Any]) -> Dict[str, Any]:
        rec.clear()
        rec.update(_empty_record(), created_by="agent" if agent_created else None)
        return {"created_by": rec["created_by"]}
    _mutate_and_emit(skill_name, "created", _apply, task_id=task_id, session_id=session_id)


def mark_agent_created(skill_name: str) -> None:
    _set_field(skill_name, "created_by", "agent")


def set_state(skill_name: str, state: str) -> None:
    """Set lifecycle state (no-op if invalid / unmanageable). Emits archived/stale/restored."""
    if state not in _VALID_STATES:
        logger.debug("set_state: invalid state %r for %s", state, skill_name)
        return

    def _apply(rec: Dict[str, Any]) -> Dict[str, Any]:
        previous = rec.get("state")
        if previous != state:
            rec["state"] = state
            if state != STATE_STALE:
                rec["archived_at"] = _now_iso() if state == STATE_ARCHIVED else None
        return {"changed": previous != state, "created_by": rec.get("created_by"), "previous_state": previous}
    facts = _mutate(skill_name, _apply, require_curation_eligible=True)
    if isinstance(facts, dict) and facts["changed"]:
        restored = state == STATE_ACTIVE and facts["previous_state"] == STATE_ARCHIVED
        action = "restored" if restored else {STATE_ARCHIVED: "archived", STATE_STALE: "stale"}.get(state)
        if action is not None:
            _emit_skill_lifecycle(skill_name, action, record=facts)


def set_pinned(skill_name: str, pinned: bool) -> bool:
    """True only when the write landed (skill curation-eligible)."""
    return _set_field(skill_name, "pinned", bool(pinned))


def set_sync(skill_name: str, sync: bool) -> None:
    _set_field(skill_name, "sync", bool(sync))


def is_sync_enabled(skill_name: str) -> bool:
    return get_record(skill_name).get("sync") is True


def forget(skill_name: str) -> None:
    if skill_name:
        _locked_update(skill_name, lambda d: (None, d.pop(skill_name, None) is not None), "skill_usage.forget(%s) failed: %s")


# --- Archive / restore ------------------------------------------------------------
def _relocate(src: Path, dest: Path, skill_name: str, action: str, **capture_kwargs: Any) -> Tuple[bool, str]:
    try:
        from curator import skill_ledger as _ledger
        _ledger_before = _ledger.capture_before(src, **capture_kwargs)
    except Exception:
        _ledger = _ledger_before = None  # type: ignore[assignment]
    try:
        src.rename(dest)
    except OSError:
        import shutil
        try:
            shutil.move(str(src), str(dest))
        except Exception as e:
            return False, f"failed to {action}: {e}"
    archiving = action == "archive"
    set_state(skill_name, STATE_ARCHIVED if archiving else STATE_ACTIVE)
    with suppress(Exception):
        if _ledger is not None:
            _ledger.record_mutation(action, skill_name, before=_ledger_before or [], after_root=dest)
    return True, f"{action}d to {dest}"


def archive_skill(skill_name: str) -> Tuple[bool, str]:
    """Move a curator-eligible skill dir to the tree's ``archive/`` (flattened; timestamp suffix on collision)."""
    skill_dir = _find_skill_dir(skill_name)
    if skill_dir is None and _find_external_skill_dir(skill_name) is not None:
        return False, _external_read_only_message(skill_name)
    if not is_curation_eligible(skill_name, skill_dir):
        return False, _external_read_only_message(skill_name)
    if skill_dir is None:
        return False, f"skill '{skill_name}' not found"
    if is_external_skill_path(skill_dir):
        return False, _external_read_only_message(skill_name)
    dest = _archive_dir() / skill_dir.name
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        return False, f"failed to create archive dir: {e}"
    if dest.exists():
        dest = dest.with_name(f"{skill_dir.name}-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}")
    return _relocate(skill_dir, dest, skill_name, "archive", complete_package=True, skill=skill_name)


def restore_skill(skill_name: str) -> Tuple[bool, str]:
    """Move an archived skill back to the flat layout."""
    archive_root = _archive_dir()
    if not archive_root.exists():
        return False, "no archive directory"
    dirs = [p for p in archive_root.rglob("*") if p.is_dir()]
    prefix = f"{skill_name}-"
    candidates = [p for p in dirs if p.name == skill_name] or sorted(
        (p for p in dirs if p.name.startswith(prefix) and len(p.name) - len(prefix) == 14
         and p.name[len(prefix):].isdigit()), reverse=True)
    if not candidates:
        return False, f"skill '{skill_name}' not found in archive"
    if (dest := _skills_dir() / skill_name).exists():
        return False, f"destination already exists: {dest}"
    return _relocate(candidates[0], dest, skill_name, "restore")


def _match_skill_dir(skill_mds: Iterable[Path], skill_name: str) -> Optional[Path]:
    return next((p.parent for p in skill_mds if _read_skill_name(p, fallback=p.parent.name) == skill_name), None)


def _find_skill_dir(skill_name: str) -> Optional[Path]:
    """Skill dir by frontmatter ``name`` (flat or nested) in the LOCAL tree."""
    from curator.skill_utils import iter_skill_index_files
    base = _skills_dir()
    return _match_skill_dir((p for p in iter_skill_index_files(base, "SKILL.md") if not is_external_skill_path(p)),
                            skill_name) if base.exists() else None


def _find_external_skill_dir(skill_name: str) -> Optional[Path]:
    from curator.skill_utils import get_all_skills_dirs
    return next((found for base in get_all_skills_dirs()[1:] if base.exists()
                 if (found := _match_skill_dir((p for p in rglob_following_symlinks(base, "SKILL.md")
                                                if not is_excluded_skill_path(p) and is_external_skill_path(p)),  # linked-in skills are local
                                               skill_name)) is not None), None)


# --- Reporting --------------------------------------------------------------------
OWNER_MANAGED, OWNER_USER, OWNER_EXTERNAL = "managed", "user", "external"


def owner_of(record: Any, *, external: bool) -> str:
    """The three ownership classes. ``managed`` = ``created_by: agent`` (the LLM pass created it, or
    the user adopted it); ``external`` = lives only under ``skills.external_dirs`` (telemetry only,
    read-only to curation); ``user`` = everything else in the curated tree — never touched."""
    if external:
        return OWNER_EXTERNAL
    return OWNER_MANAGED if _is_curator_managed_record(record) else OWNER_USER


def owner(skill_name: str, record: Optional[Dict[str, Any]] = None) -> str:
    if record is None:
        record = load_usage().get(skill_name)
    return owner_of(record, external=_find_skill_dir(skill_name) is None and _find_external_skill_dir(skill_name) is not None)


def curated_report() -> List[Dict[str, Any]]:
    """One backfilled row per curator-managed skill (plus pinned local ones) with ``owner`` and ``_persisted``."""
    data = load_usage()
    names = set(list_agent_created_skill_names())
    names.update(name for name, rec in data.items()
                 if rec.get("pinned") and is_curation_eligible(name) and _find_skill_dir(name) is not None)
    return [_report_row(n, data.get(n), _persisted=n in data, owner=owner_of(data.get(n), external=False)) for n in sorted(names)]


def usage_report() -> List[Dict[str, Any]]:
    """Usage rows for EVERY skill the curator scans: the curated tree plus ``skills.external_dirs``."""
    from curator.skill_utils import get_all_skills_dirs
    data = load_usage()
    seen: Dict[str, bool] = {}  # name -> external?  (curated tree first, so a linked skill reads as local)
    for i, base in enumerate(get_all_skills_dirs()):
        if not base.exists():
            continue
        for name, skill_md in _iter_skill_mds(base, local_only=False):
            seen.setdefault(name, i > 0 or is_external_skill_path(skill_md))
    return [_report_row(n, data.get(n), owner=owner_of(data.get(n), external=ext), _persisted=n in data)
            for n, ext in sorted(seen.items())]
