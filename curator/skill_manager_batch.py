"""Atomic multi-op batch path for ``skill_manage`` (port of ``tools/skill_manager_batch.py``).

Every touched skill is snapshotted first and any failure rolls ALL of them
back (batch-created skills are removed). ``delete`` is only legal as the SOLE
op (its recoverable-archive path doesn't compose with rollback).
"""

from __future__ import annotations

import json
import logging
import posixpath
import shutil
import tempfile
from pathlib import Path

logger = logging.getLogger("curator.skill_manager")

_BATCH_OP_ACTIONS = {"create", "patch", "write_file", "remove_file"}
_BATCH_MAX_OPS = 20


def _validate_batch_ops(operations, default_name, tool_error):
    from curator.skill_manager_guards import _background_review_preflight

    def fail(i, msg):
        return None, tool_error(f"operations[{i}]{msg}", success=False)
    names = []
    for i, op in enumerate(operations):
        if not isinstance(op, dict) or not op.get("action"):
            return fail(i, " needs an 'action'.")
        act = op["action"]
        if act not in _BATCH_OP_ACTIONS:
            return fail(i, f": unknown action '{act}'. Batchable: {', '.join(sorted(_BATCH_OP_ACTIONS))}; delete must be sole.")
        nm = op.get("name") or default_name
        if not nm:
            return fail(i, " needs a 'name' (the skill it targets).")
        names.append(nm)
        if act == "create" and nm in names[:-1]:
            return fail(i, f": create for '{nm}' must precede that skill's other ops.")
        if (preflight := _background_review_preflight(act, nm)) is not None:
            return None, json.dumps(preflight, ensure_ascii=False)
    touched_files = set()
    for i, op in enumerate(operations):
        act, nm = op["action"], names[i]
        full_rewrite = act == "patch" and bool(op.get("content"))
        fp = (op.get("file_path") or "").strip()
        target = ("SKILL.md" if (act == "create" or full_rewrite or not fp) else posixpath.normpath(fp.lstrip("/")))
        key = (nm, target)
        if (act in ("create", "write_file", "remove_file") or full_rewrite) and key in touched_files:
            return fail(i, f": {act} on '{target}' of skill '{nm}' — an earlier op in this batch already touched that file, "
                           f"and this op would silently discard its work. One destructive op (write_file/remove_file/full "
                           f"rewrite) per file per batch; put it first, or fold the change in. Patch chains are fine.")
        touched_files.add(key)
    return names, None


def _snapshot_skills(names, snap_root, find_skill):
    snapshots = {}
    for nm in dict.fromkeys(names):
        pre = find_skill(nm)
        pre_dir = Path(pre["path"]) if pre else None
        snap = snap_root / nm if pre_dir is not None and pre_dir.is_dir() else None
        if snap is not None:
            try:
                shutil.copytree(pre_dir, snap)
            except Exception as exc:  # noqa: BLE001
                return None, f"Could not snapshot '{nm}' for atomic batch: {exc}"
        snapshots[nm] = (pre_dir, snap)
    return snapshots, None


def _restore_snapshot(pre_dir, snap, post_dir) -> None:
    post_exists = post_dir is not None and post_dir.is_dir()
    if snap is None:
        if post_exists:
            shutil.rmtree(post_dir)
        return
    if not post_exists:
        shutil.copytree(snap, pre_dir)
        return
    aside = post_dir.with_name(post_dir.name + ".rollback-broken")
    shutil.rmtree(aside, ignore_errors=True)
    post_dir.rename(aside)
    try:
        shutil.copytree(snap, pre_dir)
    except Exception:
        shutil.rmtree(pre_dir, ignore_errors=True)
        aside.rename(pre_dir)
        raise
    shutil.rmtree(aside, ignore_errors=True)


def _rollback(snapshots, find_skill):
    notes = []
    for nm, (pre_dir, snap) in snapshots.items():
        try:
            post = find_skill(nm)
            _restore_snapshot(pre_dir, snap, Path(post["path"]) if post else None)
        except Exception as exc:  # noqa: BLE001
            notes.append(f"ROLLBACK FAILED for '{nm}' ({exc})" + (f"; snapshot preserved at '{snap}'" if snap is not None else ""))
    return ("; ".join(notes) if notes else "all touched skills rolled back"), bool(notes)


def _skill_manage_batch(operations, default_name=None, task_id=None, session_id=None) -> str:
    from curator import skill_manager as _smt
    from curator.skill_manager import tool_error
    if not isinstance(operations, list) or not operations:
        return tool_error("operations must be a non-empty array.", success=False)
    if len(operations) > _BATCH_MAX_OPS:
        return tool_error(f"operations is capped at {_BATCH_MAX_OPS} ops per call.", success=False)
    if any(isinstance(op, dict) and op.get("action") == "delete" for op in operations):
        if len(operations) != 1:
            return tool_error("delete must be the SOLE op in its call — it doesn't compose with other ops' rollback.",
                              success=False)
        nm = operations[0].get("name") or default_name
        if not nm:
            return tool_error("operations[0] (delete) needs a 'name'.", success=False)
        return _smt.skill_manage(action="delete", name=nm, task_id=task_id, session_id=session_id,
                                 absorbed_into=operations[0].get("absorbed_into"))
    names, err = _validate_batch_ops(operations, default_name, tool_error)
    if err is not None:
        return err
    if not _smt._skill_gate_bypass.get():
        staged = _smt._run_write_gate(lambda wa: None)
        if staged is not None:
            return staged
    snap_root = Path(tempfile.mkdtemp(prefix="skill_batch_"))
    snapshots, snap_err = _snapshot_skills(names, snap_root, _smt._find_skill)
    if snap_err is not None:
        shutil.rmtree(snap_root, ignore_errors=True)
        return tool_error(snap_err, success=False)
    results = []
    rollback_failed = False
    token = _smt._skill_gate_bypass.set(True)
    try:
        for i, op in enumerate(operations):
            raw = _smt._skill_manage_from({**op, "name": names[i], "operations": None}, task_id=task_id, session_id=session_id)
            try:
                parsed = json.loads(raw)
            except Exception:  # noqa: BLE001
                parsed = {"success": False, "error": "unparseable op result"}
            if not parsed.get("success"):
                note, rollback_failed = _rollback(snapshots, _smt._find_skill)
                fail = {"success": False,
                        "error": (f"operations[{i}] ({op['action']} on '{names[i]}') failed: "
                                  f"{parsed.get('error', 'unknown error')} — batch aborted, {note}."),
                        "failed_index": i, "completed_before_failure": i}
                for k, v in parsed.items():
                    if k not in ("success", "error") and v is not None:
                        fail.setdefault(k, v)
                return json.dumps(fail, ensure_ascii=False)
            results.append({"name": names[i], "action": op["action"], "file_path": op.get("file_path"), "success": True})
    finally:
        _smt._skill_gate_bypass.reset(token)
        if rollback_failed:
            logger.warning("skill_manage batch rollback failed, snapshots kept at %s", snap_root)
        else:
            shutil.rmtree(snap_root, ignore_errors=True)
    return json.dumps({"success": True, "operations_applied": len(results), "results": results}, ensure_ascii=False)
