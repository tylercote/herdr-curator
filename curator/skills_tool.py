"""``skills_list`` / ``skill_view`` — the read half of the skills toolset (port of the
curator-relevant subset of ``tools/skills_tool.py``).

``skill_view`` is also the read-before-write authorisation for the background
review fork: it marks the exact file it served so a later ``skill_manage`` on
that file is allowed. ``skill_view_with_bump`` (the handler the MCP server
registers) additionally records ``view`` + ``use`` telemetry, exactly like the
Hermes tool registration does.

Not ported (Hermes-runtime concerns): plugin-provided skills, SKILL.md template
variables / inline shell preprocessing, readiness (config-var) checks, org
provenance headers, project-skill quarantine, and the per-session repeat-view
dedup stub.
"""

from __future__ import annotations

import json
import logging
from contextlib import suppress
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Dict, List, Optional, Tuple

from curator import paths
from curator.skill_utils import (
    EXCLUDED_SKILL_DIRS, get_disabled_skill_names, get_external_skills_dirs, get_project_skills_dirs,
    iter_skill_index_files, parse_frontmatter as _parse_frontmatter, skill_matches_platform)

logger = logging.getLogger(__name__)

MAX_NAME_LENGTH = 64
_LOOKUP_HINT = "Use a skill name or relative path within the skills directory."
_LINKED_FILE_SPECS = (("references", ("*",), True, True), ("templates", ("*",), True, True),
                      ("scripts", ("*",), True, True), ("assets", ("*",), True, True))
_INJECTION_PATTERNS = ("ignore previous instructions", "ignore all previous", "disregard your instructions",
                       "you are now", "new instructions:", "system prompt:")


def _json(payload: Dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False)


def _fail(message: str, **extra: Any) -> str:
    return _json({"success": False, "error": message, **{k: v for k, v in extra.items() if v is not None}})


def _skills_dir() -> Path:
    return paths.skills_dir()


def _read_skill_text(path: Path) -> str:
    return path.read_text(encoding="utf-8-sig", errors="replace")


def _skill_lookup_path_error(name: str) -> Optional[str]:
    from curator.path_security import has_traversal_component
    if not isinstance(name, str):
        return "Skill name must be a string."
    win = PureWindowsPath(candidate := name.strip())
    if PurePosixPath(candidate).is_absolute() or win.is_absolute() or win.drive:
        return "Skill name must be a relative path within the skills directory."
    if has_traversal_component(candidate):
        return "Skill name cannot contain '..' path traversal components."
    return None


def _parse_tags(tags_value) -> List[str]:
    if not tags_value:
        return []
    if isinstance(tags_value, list):
        return [str(t).strip() for t in tags_value if t]
    tags_value = str(tags_value).strip()
    if tags_value.startswith("[") and tags_value.endswith("]"):
        tags_value = tags_value[1:-1]
    return [t.strip().strip("\"'") for t in tags_value.split(",") if t.strip()]


def _is_skill_disabled(name: str) -> bool:
    try:
        return name in get_disabled_skill_names()
    except Exception:
        return False


def _skill_search_dirs() -> Tuple[list, list, Path]:
    project_dirs = list(get_project_skills_dirs())
    active_skills_dir = _skills_dir()
    all_dirs = project_dirs + ([active_skills_dir] if active_skills_dir.exists() else [])
    all_dirs += get_external_skills_dirs()
    return project_dirs, all_dirs, active_skills_dir


def _get_category_from_path(skill_md: Path) -> Optional[str]:
    for root in (d for d in _skill_search_dirs()[1]):
        try:
            rel = skill_md.parent.relative_to(root)
        except ValueError:
            continue
        return rel.parts[0] if len(rel.parts) > 1 else None
    return None


def _truncate_description(description: str) -> str:
    text = " ".join(str(description).split())
    return text[:1024]


def _find_all_skills(*, skip_disabled: bool = False) -> List[Dict[str, Any]]:
    """All skills (name, description, category) across project/local/external dirs, first-wins by name."""
    disabled = set() if skip_disabled else get_disabled_skill_names()
    _project_dirs, dirs_to_scan, _ = _skill_search_dirs()
    skills = []
    seen_names: set = set()
    for scan_dir in dirs_to_scan:
        for skill_md in iter_skill_index_files(scan_dir, "SKILL.md"):
            if any(part in EXCLUDED_SKILL_DIRS for part in skill_md.parts):
                continue
            try:
                frontmatter, body = _parse_frontmatter(_read_skill_text(skill_md)[:4000])
                if not skill_matches_platform(frontmatter):
                    continue
                name = str(frontmatter.get("name", skill_md.parent.name))[:MAX_NAME_LENGTH]
                if name in seen_names or name in disabled:
                    continue
                description = frontmatter.get("description", "")
                if not description:
                    description = next((ln for ln in map(str.strip, body.strip().split("\n")) if ln and not ln.startswith("#")),
                                       description)
                seen_names.add(name)
                skills.append({"name": name, "description": _truncate_description(description or ""),
                               "category": _get_category_from_path(skill_md)})
            except Exception as e:
                logger.debug("Skipping skill at %s: failed to parse: %s", skill_md, e, exc_info=True)
    return skills


def _sort_skills(skills: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return sorted(skills, key=lambda s: (s.get("category") or "", s["name"]))


def skills_list(category: Optional[str] = None, task_id: Optional[str] = None) -> str:
    try:
        _skills_dir().mkdir(parents=True, exist_ok=True)
        all_skills = _find_all_skills()
        if not all_skills:
            return _json({"success": True, "skills": [], "categories": [], "message": "No skills found in skills/ directory."})
        if category:
            all_skills = [s for s in all_skills if s.get("category") == category]
        all_skills = _sort_skills(all_skills)
        categories = sorted({s.get("category") for s in all_skills if s.get("category")})
        return _json({"success": True, "skills": all_skills, "categories": categories, "count": len(all_skills),
                      "hint": "Use skill_view(name) to see full content, tags, and linked files"})
    except Exception as e:
        return _fail(str(e))


def _skill_linked_files(skill_dir: Optional[Path]) -> dict:
    files: dict = {}
    for sub, globs, recursive, files_only in _LINKED_FILE_SPECS if skill_dir else ():
        base = skill_dir / sub
        found = sorted(str(f.relative_to(skill_dir)) for g in globs if base.exists()
                       for f in (base.rglob(g) if recursive else base.glob(g)) if not files_only or f.is_file())
        if found:
            files[sub] = found
    return files


def _under_any(path: Path, roots) -> bool:
    try:
        resolved = path.resolve()
    except OSError:
        resolved = path
    for root in roots:
        try:
            if resolved.is_relative_to(Path(root).resolve()):
                return True
        except OSError:
            continue
    return False


def _collect_skill_candidates(name: str, all_dirs) -> List[Tuple[Path, Path]]:
    """``(skill_dir, skill_md)`` for *name* as a bare name (frontmatter or dir name) or relative path."""
    out: List[Tuple[Path, Path]] = []
    seen: set = set()
    for base in all_dirs:
        direct = base / name / "SKILL.md"
        if direct.exists() and direct.resolve() not in seen:
            seen.add(direct.resolve())
            out.append((direct.parent, direct))
            continue
        for skill_md in iter_skill_index_files(base, "SKILL.md"):
            if skill_md.resolve() in seen:
                continue
            fm, _ = _parse_frontmatter(_read_skill_text(skill_md)[:4000])
            if fm.get("name") == name or skill_md.parent.name == name:
                seen.add(skill_md.resolve())
                out.append((skill_md.parent, skill_md))
    return out


def _locate_skill(name: str, project_dirs: list, all_dirs):
    if not all_dirs:
        return _fail("Skills directory does not exist yet. It will be created on first install."), None, None
    candidates = _collect_skill_candidates(name, all_dirs)
    if len(candidates) > 1 and project_dirs:
        candidates = [c for c in candidates if _under_any(c[1], project_dirs)] or candidates
    if len(candidates) > 1:
        skill_paths = [str(smd) for _, smd in candidates]
        logger.warning("Skill name collision for '%s': %d candidates — %s", name, len(candidates), "; ".join(skill_paths))
        return _fail(
            f"Ambiguous skill name '{name}': {len(candidates)} skills match across your local skills dir "
            "and external_dirs. Refusing to guess — load one explicitly by its categorized path.",
            matches=skill_paths,
            hint="Pass the full relative path instead of the bare name (e.g., 'category/skill-name'), "
                 "or rename one of the colliding skills so each name is unique."), None, None
    skill_dir, skill_md = candidates[0] if candidates else (None, None)
    if not skill_md or not skill_md.exists():
        available = [s["name"] for s in _sort_skills(_find_all_skills())[:20]]
        return _fail(f"Skill '{name}' not found.", available_skills=available,
                     hint="Use skills_list to see all available skills"), None, None
    return None, skill_dir, skill_md


def _log_security_warnings(name: str, skill_md: Path, content: str, all_dirs, active_skills_dir) -> None:
    trusted_dirs = [active_skills_dir]
    with suppress(Exception):
        trusted_dirs.extend(all_dirs)
    warnings = []
    if not _under_any(skill_md, trusted_dirs):
        warnings.append(f"skill file is outside the trusted skills directory: {skill_md}")
    if any(p in content.lower() for p in _INJECTION_PATTERNS):
        warnings.append("skill content contains patterns that may indicate prompt injection")
    if warnings:
        logger.warning("Skill security warning for '%s': %s", name, "; ".join(warnings))


def _mark_background_review_read(path: Path) -> None:
    with suppress(Exception):
        from curator.skill_manager_guards import mark_background_review_skill_read
        mark_background_review_skill_read(path)


def _serve_skill_file(skill_dir: Path, file_path: str, name: str, *, list_available: bool, mark_read: bool, hint: str) -> str:
    from curator.path_security import validate_within_dir
    target = skill_dir / file_path
    if err := validate_within_dir(target, skill_dir):
        return _fail(err, hint=hint)
    if not target.is_file():
        available = None
        if list_available:
            available = [str(f.relative_to(skill_dir)) for sub, *_ in _LINKED_FILE_SPECS
                         if (skill_dir / sub).exists() for f in sorted((skill_dir / sub).rglob("*")) if f.is_file()]
        return _fail(f"File '{file_path}' not found in skill '{name}'.", available_files=available, hint=hint)
    try:
        content = _read_skill_text(target)
    except Exception as e:
        return _fail(f"Failed to read file '{file_path}': {e}")
    if mark_read:
        _mark_background_review_read(target)
    return _json({"success": True, "name": name, "file_path": file_path, "content": content, "path": str(target)})


def skill_view(name: str, file_path: Optional[str] = None, task_id: Optional[str] = None, preprocess: bool = True) -> str:
    """View a skill (SKILL.md) or a file within its directory, as JSON."""
    try:
        if lookup_error := _skill_lookup_path_error(name):
            return _fail(lookup_error, hint=_LOOKUP_HINT)
        project_dirs, all_dirs, active_skills_dir = _skill_search_dirs()
        error, skill_dir, skill_md = _locate_skill(name.strip(), project_dirs, all_dirs)
        if error is not None:
            return error
        try:
            content = _read_skill_text(skill_md)
        except Exception as e:
            return _fail(f"Failed to read skill '{name}': {e}")
        _log_security_warnings(name, skill_md, content, all_dirs, active_skills_dir)
        frontmatter, _body = _parse_frontmatter(content)
        if not skill_matches_platform(frontmatter):
            return _fail(f"Skill '{name}' is not supported on this platform.")
        resolved_name = frontmatter.get("name", skill_md.parent.name)
        if _is_skill_disabled(resolved_name):
            return _fail(f"Skill '{resolved_name}' is disabled. Enable it in config (skills.disabled) or inspect the files directly on disk.")
        if file_path and skill_dir:
            return _serve_skill_file(skill_dir, file_path, name, list_available=True, mark_read=True,
                                     hint="Use a relative path within the skill directory")
        metadata = frontmatter.get("metadata")
        hermes_meta = (metadata.get("hermes", {}) or {}) if isinstance(metadata, dict) else {}
        tags, related_skills = (_parse_tags(hermes_meta.get(k) or frontmatter.get(k, "")) for k in ("tags", "related_skills"))
        linked_files = _skill_linked_files(skill_dir)
        try:
            rel_path = str(skill_md.relative_to(active_skills_dir))
        except ValueError:
            rel_path = str(skill_md.relative_to(skill_md.parent.parent)) if skill_md.parent.parent else skill_md.name
        skill_name = frontmatter.get("name", skill_dir.name if skill_dir else skill_md.stem)
        result = {
            "success": True, "name": skill_name, "description": frontmatter.get("description", ""),
            "tags": tags, "related_skills": related_skills, "content": content,
            "path": rel_path, "skill_dir": str(skill_dir) if skill_dir else None,
            "linked_files": linked_files if linked_files else None,
            "usage_hint": ("To view linked files, call skill_view(name, file_path) where file_path is e.g. "
                           "'references/api.md' or 'assets/config.yaml'") if linked_files else None,
            "_source_path": str(skill_md)}
        _mark_background_review_read(skill_md)
        if frontmatter.get("compatibility"):
            result["compatibility"] = frontmatter["compatibility"]
        if isinstance(metadata, dict):
            result["metadata"] = metadata
        return _json(result)
    except Exception as e:
        return _fail(str(e))


def skill_view_with_bump(args: Dict[str, Any], **kw) -> str:
    """Invoke skill_view, then bump view_count + use on success (best-effort). Viewing is actively
    loading the skill to act on it — that counts as use (the curator's stale timer keys off last_used_at)."""
    name = args.get("name", "")
    result = skill_view(name, file_path=args.get("file_path"), task_id=kw.get("task_id"))
    with suppress(Exception):
        parsed = json.loads(result)
        if isinstance(parsed, dict) and parsed.get("success"):
            if resolved := parsed.get("name") or name:
                from curator.skill_usage import bump_use, bump_view
                bump_view(str(resolved))
                bump_use(str(resolved), task_id=kw.get("task_id"), session_id=kw.get("session_id"))
    return result


SKILLS_LIST_SCHEMA = {
    "name": "skills_list",
    "description": "List available skills (name + description + category). Use skill_view(name) for the full content.",
    "parameters": {"type": "object", "properties": {"category": {"type": "string", "description": "Only skills in this category subdir."}}},
}

SKILL_VIEW_SCHEMA = {
    "name": "skill_view",
    "description": ("View a skill's SKILL.md (frontmatter, body, linked support files) or, with file_path, one of its "
                    "references/templates/scripts/assets files. Reading the exact target is REQUIRED before any "
                    "skill_manage write to it during a curator pass."),
    "parameters": {"type": "object",
                   "properties": {"name": {"type": "string", "description": "Skill name or categorized relative path."},
                                  "file_path": {"type": "string", "description": "Optional support file relative to the skill dir, e.g. 'references/api.md'."}},
                   "required": ["name"]},
}
