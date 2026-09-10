"""Scheduled-job store — the subset the curator uses.

Jobs live in ``<home>/cron/jobs.json`` as ``{"jobs": [...], "updated_at": ...}``.
The curator needs two things from it:

* ``referenced_skill_names()`` — skills any job (paused or not) lists, so the
  inactivity prune never archives a skill out from under a schedule;
* ``rewrite_skill_refs()`` — after a consolidation, point jobs at the umbrella
  (and drop pruned names) so they keep loading the right instructions.

Herdr has no scheduler of its own, so this file is also the format a user (or
another plugin) can write to declare "this skill is in use by an automation".
Locking: in-process RLock + cross-process flock on ``<cron>/.jobs.lock``.
"""

from __future__ import annotations

import contextlib
import json
import logging
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

from curator import paths
from curator.fsutil import atomic_write_text

logger = logging.getLogger(__name__)

try:
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None

_JOBS_LOCK_TIMEOUT_SECONDS = 5.0
_jobs_file_lock = threading.RLock()
_jobs_lock_state = threading.local()


def cron_dir() -> Path:
    return paths.cron_dir()


def jobs_file() -> Path:
    return paths.cron_jobs_file()


def ensure_dirs() -> None:
    cron_dir().mkdir(parents=True, exist_ok=True)


def _acquire_flock(lock_fd, timeout: float) -> Optional[bool]:
    if fcntl is None:
        return None
    deadline = time.monotonic() + timeout
    while True:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except (OSError, IOError):
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.05)


@contextlib.contextmanager
def _jobs_lock():
    """Serialize a load→modify→save section. Nested calls in one thread reuse the held lock."""
    depth = getattr(_jobs_lock_state, "depth", 0)
    if depth:
        _jobs_lock_state.depth = depth + 1
        try:
            yield
        finally:
            _jobs_lock_state.depth -= 1
        return
    with _jobs_file_lock:
        _jobs_lock_state.depth = 1
        lock_fd = None
        try:
            try:
                ensure_dirs()
                lock_fd = open(cron_dir() / ".jobs.lock", "a+", encoding="utf-8")
                if _acquire_flock(lock_fd, _JOBS_LOCK_TIMEOUT_SECONDS) is False:
                    logger.error("jobs.json cross-process lock timed out; proceeding with in-process lock only")
            except OSError as e:
                logger.debug("jobs.json lock unavailable (%s); in-process lock only", e)
            yield
        finally:
            _jobs_lock_state.depth = 0
            if lock_fd is not None:
                with contextlib.suppress(Exception):
                    if fcntl is not None:
                        fcntl.flock(lock_fd, fcntl.LOCK_UN)
                    lock_fd.close()


def _parse_jobs_file(path: Path) -> Any:
    raw = path.read_text(encoding="utf-8-sig")
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return json.loads(raw, strict=False)


def load_jobs() -> List[Dict[str, Any]]:
    """Load all jobs; the canonical dict, an id-keyed map, or a bare list are all accepted
    (the latter two are auto-repaired on disk)."""
    path = jobs_file()
    ensure_dirs()
    if not path.exists():
        return []
    try:
        data = _parse_jobs_file(path)
    except IOError as e:
        raise RuntimeError(f"Failed to read cron database: {e}") from e
    except Exception as e:
        raise RuntimeError(f"Cron database corrupted and unrepairable: {e}") from e
    repair = None
    if isinstance(data, dict):
        jobs = data.get("jobs", [])
        if isinstance(jobs, dict):
            jobs = [{**v, "id": v.get("id") or k} for k, v in jobs.items() if isinstance(v, dict)]
            repair = "id-keyed jobs map flattened to list"
    elif isinstance(data, list):
        jobs = data
        repair = "bare list wrapped as dict"
    else:
        raise RuntimeError(f"Cron database corrupted: expected {{'jobs': [...]}}, got {type(data).__name__}")
    if jobs and repair:
        save_jobs(jobs)
        logger.warning("Auto-repaired jobs.json (%s)", repair)
    return jobs


def save_jobs(jobs: List[Dict[str, Any]]) -> None:
    with _jobs_lock():
        ensure_dirs()
        payload = {"jobs": jobs, "updated_at": datetime.now(timezone.utc).isoformat()}
        atomic_write_text(jobs_file(), json.dumps(payload, indent=2, ensure_ascii=False), tmp_prefix=".jobs_")


def _normalize_skill_list(skill: Optional[str] = None, skills: Optional[Any] = None) -> List[str]:
    if skills is None:
        raw_items = [skill] if skill else []
    elif isinstance(skills, str):
        raw_items = [skills]
    else:
        raw_items = list(skills)
    normalized: List[str] = []
    for item in raw_items:
        text = str(item or "").strip()
        if text and text not in normalized:
            normalized.append(text)
    return normalized


def _canonical_skill_ref(raw: Any) -> str:
    """Reduce a job skill reference (possibly an absolute path) to the bare name the curator matches on."""
    value = str(raw or "").strip()
    if not value:
        return ""
    try:
        from curator.skill_utils import normalize_skill_lookup_name
        value = normalize_skill_lookup_name(value) or value
    except Exception:
        logger.debug("referenced_skill_names: could not normalize skill ref %r", raw, exc_info=True)
    return value.strip().lstrip("/")


def create_job(prompt: str = "", schedule: str = "", *, name: Optional[str] = None, skills: Optional[Any] = None,
               skill: Optional[str] = None, enabled: bool = True, **extra: Any) -> Dict[str, Any]:
    """Minimal job creation (the curator never creates jobs; tests and other plugins do)."""
    skill_list = _normalize_skill_list(skill, skills)
    job: Dict[str, Any] = {"id": uuid.uuid4().hex[:8], "name": name or (prompt[:40] if prompt else "job"),
                           "prompt": prompt, "schedule": schedule, "enabled": enabled,
                           "created_at": datetime.now(timezone.utc).isoformat(),
                           "skills": skill_list, "skill": skill_list[0] if skill_list else None, **extra}
    with _jobs_lock():
        jobs = load_jobs()
        jobs.append(job)
        save_jobs(jobs)
    return job


def get_job(job_id: str) -> Optional[Dict[str, Any]]:
    return next((j for j in load_jobs() if isinstance(j, dict) and j.get("id") == job_id), None)


def referenced_skill_names() -> Set[str]:
    """Skill names referenced by ANY job, including paused/disabled ones. Corrupt store -> empty set."""
    try:
        jobs = load_jobs()
    except Exception:
        logger.debug("referenced_skill_names: failed to load cron jobs", exc_info=True)
        return set()
    return {cleaned for job in jobs if isinstance(job, dict)
            for name in _normalize_skill_list(job.get("skill"), job.get("skills"))
            if (cleaned := _canonical_skill_ref(name))}


def rewrite_skill_refs(consolidated: Optional[Dict[str, str]] = None, pruned: Optional[List[str]] = None) -> Dict[str, Any]:
    """Rewrite job skill references after a curator pass: consolidated names map to their umbrella
    (deduped, order preserved), pruned names are dropped, the legacy ``skill`` field realigned."""
    consolidated = dict(consolidated or {})
    pruned_set = set(pruned or []) - set(consolidated.keys())
    if not consolidated and not pruned_set:
        return {"rewrites": [], "jobs_updated": 0, "jobs_scanned": 0}
    with _jobs_lock():
        jobs = load_jobs()
        rewrites: List[Dict[str, Any]] = []
        for job in jobs:
            if not isinstance(job, dict):
                continue
            skills_before = _normalize_skill_list(job.get("skill"), job.get("skills"))
            if not skills_before:
                continue
            mapped: Dict[str, str] = {}
            dropped: List[str] = []
            new_skills: List[str] = []
            for name in skills_before:
                if name in consolidated:
                    target = consolidated[name]
                    mapped[name] = target
                    if target and target not in new_skills:
                        new_skills.append(target)
                elif name in pruned_set:
                    dropped.append(name)
                elif name not in new_skills:
                    new_skills.append(name)
            if not mapped and not dropped:
                continue
            job["skills"] = new_skills
            job["skill"] = new_skills[0] if new_skills else None
            rewrites.append({"job_id": job.get("id"), "job_name": job.get("name") or job.get("id"),
                             "before": list(skills_before), "after": list(new_skills), "mapped": mapped, "dropped": dropped})
        if rewrites:
            save_jobs(jobs)
            logger.info("Curator rewrote skill references in %d cron job(s)", len(rewrites))
        return {"rewrites": rewrites, "jobs_updated": len(rewrites), "jobs_scanned": len(jobs)}
