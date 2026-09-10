"""Write/delete guards for ``skill_manage`` (port of ``tools/skill_manager_guards.py``).

Every guard returns ``None`` when the operation may proceed, else a refusal
(error dict or message). Origin-owned state (``_find_skill``, ``_skills_dir``)
is reached lazily via ``curator.skill_manager`` so test patches keep working.

The background-review guards are what make the LLM consolidation pass safe:

* ownership — the fork may only touch curator-managed sediment (never pinned,
  external or user-owned skills);
* read-before-write — a mutation must be preceded by ``skill_view`` of the
  SAME file in the same review (marks are per-review, shared across copied
  tool contexts);
* consolidation delete — a delete needs ``absorbed_into=<existing umbrella>``;
  bare prunes are refused and the skill stays active.
"""

from __future__ import annotations

import contextvars as _ctxvars
import logging
import threading
from contextlib import suppress
from pathlib import Path
from typing import Any, Dict, Optional

from curator.prog import cmd as _cmd

logger = logging.getLogger("curator.skill_manager")


def _refusal(message: str, **extra: Any) -> Dict[str, Any]:
    return {"success": False, "error": message, **extra}


def _is_background_review() -> bool:
    try:
        from curator.skill_provenance import is_background_review
        return bool(is_background_review())
    except Exception:
        return False


def _resolved_str(path: Path) -> str:
    with suppress(Exception):
        return str(path.resolve())
    return str(path)


class _BackgroundReviewReadMarks:
    """Read marks shared by copied tool contexts within one review run."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._paths: set = set()

    def add(self, path: str) -> None:
        with self._lock:
            self._paths.add(path)

    def contains(self, path: str) -> bool:
        with self._lock:
            return path in self._paths


_background_review_read_paths: _ctxvars.ContextVar = _ctxvars.ContextVar("background_review_read_paths", default=None)


def mark_background_review_skill_read(path: Path) -> None:
    """Record that the active background-review fork has read a skill file."""
    if not _is_background_review():
        return
    if (marks := _background_review_read_paths.get()) is None:
        _background_review_read_paths.set(marks := _BackgroundReviewReadMarks())
    marks.add(_resolved_str(path))


def _background_review_has_read(path: Path) -> bool:
    marks = _background_review_read_paths.get()
    return marks is not None and marks.contains(_resolved_str(path))


def _reset_background_review_read_marks() -> None:
    _background_review_read_paths.set(_BackgroundReviewReadMarks())


def _resolved_roots(skill_path: Path):
    from curator.skill_utils import get_all_skills_dirs
    try:
        resolved = skill_path.resolve()
    except OSError:
        resolved = skill_path
    roots = []
    for root in get_all_skills_dirs():
        with suppress(OSError):
            roots.append((root, root.resolve()))
    return resolved, roots


def _containing_skills_root(skill_path: Path) -> Path:
    from curator import skill_manager as _smt
    resolved, roots = _resolved_roots(skill_path)
    return next((root for root, r in roots if resolved.is_relative_to(r)), _smt._skills_dir())


def _is_path_redirect(path: Path) -> bool:
    try:
        return path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction())
    except OSError:
        return False


def _validate_delete_target(skill_dir: Path) -> Optional[str]:
    """Last-line guard before rmtree: never delete a path outside every known skills root, a
    skills root itself, or a symlink/junction (rmtree follows it)."""
    if _is_path_redirect(skill_dir):
        return (f"Refusing to delete '{skill_dir}': the skill directory is a "
                f"symlink/junction. Remove the link target manually if intended.")
    try:
        skill_dir.resolve()
    except OSError as exc:
        return f"Refusing to delete '{skill_dir}': could not resolve path ({exc})."
    resolved, roots = _resolved_roots(skill_dir)
    for _root, root in roots:
        if resolved == root:
            return (f"Refusing to delete '{skill_dir}': resolves to the skills root "
                    f"itself, which would remove every installed skill.")
        if resolved.is_relative_to(root):
            return None
    return f"Refusing to delete '{skill_dir}': path does not resolve inside any known skills root."


def _is_pinned(name: str, what: str) -> Optional[bool]:
    try:
        from curator import skill_usage
        return bool(skill_usage.get_record(name).get("pinned"))
    except Exception:
        logger.debug("%s lookup failed for %s", what, name, exc_info=True)
        return None


def _pinned_guard(name: str) -> Optional[str]:
    """Delete-time refusal for essential skills (edits stay allowed) and pinned ones. Pinned skills are
    already refused for every action by ``_ownership_write_guard``; this is the delete-path backstop."""
    try:
        from curator.skill_utils import ESSENTIAL_SKILLS
        if name in ESSENTIAL_SKILLS:
            return (f"Skill '{name}' is essential (the agent's own operating manual referenced by the "
                    f"system prompt) and cannot be deleted. Patches and edits are still allowed.")
    except Exception:
        logger.debug("essential-guard lookup failed for %s", name, exc_info=True)
    if _is_pinned(name, "pinned-guard"):
        return (f"Skill '{name}' is pinned and cannot be deleted by skill_manage. Ask the user to "
                f"run `{_cmd('unpin ' + name)}` if they want it changed.")
    return None


def _ownership_write_guard(name: str, skill_dir: Path, action: str) -> Optional[Dict[str, Any]]:
    """Refuse ``skill_manage`` writes to anything but curator-managed skills. Unconditional: the
    write origin flavours telemetry (ledger actor, view-vs-use, archive-vs-delete) but never
    decides whether ownership is checked, so no caller can forget to bind it."""
    refuse = f"Refusing skill_manage {action} for"
    name = skill_dir.name  # usage records are keyed by the skill dir name; callers may pass `category/name`
    if _is_pinned(name, "pinned skill guard"):
        return _refusal(f"{refuse} pinned skill '{name}': pinned skills are off-limits to skill_manage. "
                        f"Ask the user to run `{_cmd('unpin ' + name)}` if they want it changed.")
    try:
        from curator.skill_utils import is_external_skill_path
        if is_external_skill_path(skill_dir):
            return _refusal(f"{refuse} skill '{name}': the skill lives in skills.external_dirs, which are "
                            f"externally owned and read-only to skill_manage.")
    except Exception:
        logger.debug("external skill guard lookup failed for %s", name, exc_info=True)
    try:
        from curator import skill_usage
        usage_rec = skill_usage.load_usage().get(name)
        if not skill_usage._is_curator_managed_record(usage_rec):
            _detail = (f"created_by={usage_rec.get('created_by')!r}" if isinstance(usage_rec, dict) else "no usage record")
            return _refusal(f"{refuse} skill '{name}': the skill is not curator-managed ({_detail}). User-owned skills "
                            f"are off-limits to skill_manage. Run `{_cmd('adopt ' + name)}` to opt it in.")
    except Exception:
        logger.warning("owned skill guard lookup failed for %s", name, exc_info=True)
        return _refusal(f"{refuse} skill '{name}': agent ownership could not be verified because the provenance "
                        f"record is unavailable or unreadable.")
    return None


def _background_review_read_before_write_guard(name: str, target: Path, action: str, file_label: str) -> Optional[Dict[str, Any]]:
    if not _is_background_review() or _background_review_has_read(target):
        return None
    return _refusal(
        f"Refusing background curator {action} for skill '{name}': the current {file_label} "
        f"content has not been loaded in this review turn. Call skill_view(name) for SKILL.md, or "
        f"skill_view(name, file_path=...) for a supporting file, then retry the write using the "
        f"content just returned.",
        _read_before_write_required=True)


def _ownership_preflight(action: str, name: str) -> Optional[Dict[str, Any]]:
    if action not in {"edit", "patch", "delete", "write_file", "remove_file"}:
        return None
    from curator import skill_manager as _smt
    existing = _smt._find_skill(name)
    return _ownership_write_guard(name, existing["path"], action) if existing else None


def _curator_consolidation_delete_guard(name: str, absorbed_into: Optional[str]) -> Optional[Dict[str, Any]]:
    """Fail closed on unverified deletes during the consolidation pass."""
    if not _is_background_review() or (isinstance(absorbed_into, str) and absorbed_into.strip()):
        return None
    return _refusal(
        f"Refusing background curator delete of skill '{name}': the consolidation pass may only "
        f"archive a skill it has absorbed into an umbrella. Pass absorbed_into=<umbrella> (the "
        f"umbrella must already exist) to record a verified consolidation. Pruning a skill with no "
        f"forwarding target is not permitted here — the deterministic inactivity prune handles "
        f"staleness archival separately. Keeping '{name}' active.",
        _fail_closed=True)


def _is_org_mirror(skill_path: Path) -> bool:
    from curator import skill_manager as _smt
    from curator.skill_utils import is_org_mirror_path
    return is_org_mirror_path(skill_path, _smt._skills_dir())


def _maybe_auto_propose_org_edit(name: str, skill_path: Path) -> Optional[str]:
    """No org skill-sync client in the plugin; org-mirror edits get no note."""
    return None


def _org_mirror_write_guard(name: str, skill_path: Path, action: str) -> Optional[Dict[str, Any]]:
    """Org-shared skills are editable in place; only deletion is refused."""
    if action not in {"delete", "remove_file"}:
        return None
    try:
        if _is_org_mirror(skill_path):
            return _refusal(f"Cannot {action} '{name}' locally: it is shared by your organisation, so a local "
                            f"delete would just come back on the next sync. Ask an org admin to remove it for "
                            f"everyone. (Editing it IS allowed.)")
    except Exception:
        logger.debug("org mirror guard lookup failed for %s", name, exc_info=True)
    return None
