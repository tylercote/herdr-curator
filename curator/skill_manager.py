"""``skill_manage`` — agent-managed skill creation & editing (port of ``tools/skill_manager_tool.py``).

Layout: ``<skills>/[category/]<skill>/SKILL.md`` + optional ``references/ templates/
scripts/ assets/``. New skills land in the local skills dir (or ``skills.create_dir``);
existing skills are modified in place wherever they live.

Every successful mutation is ledgered (before/after blobs) and reflected in the
usage sidecar; the background-review guards make the same function safe to hand
to the autonomous consolidation fork.

Deliberately NOT ported (Hermes-runtime concerns; see docs/PARITY.md): the
security scanner (``skills.guard_agent_created``), the staged write-approval
gate (``skills.write_approval``), advisory lint findings, the sync push, and
other-profile lookups in not-found errors.
"""

from __future__ import annotations

import contextvars as _ctxvars
import json
import logging
import re
import shutil
from contextlib import suppress
from pathlib import Path
from typing import Any, Dict, Optional

from curator import paths
from curator.fsutil import atomic_write_text
from curator.skill_manager_batch import _skill_manage_batch
from curator.skill_manager_guards import (
    _background_review_preflight, _background_review_read_before_write_guard, _background_review_write_guard,
    _containing_skills_root, _curator_consolidation_delete_guard, _is_background_review, _maybe_auto_propose_org_edit,
    _org_mirror_write_guard, _pinned_guard, _refusal as _err, _validate_delete_target)
from curator.skill_utils import (
    SKILL_PROMPT_DESC_LIMIT, display_skill_create_dir, extract_skill_description,
    is_skill_description_truncated_for_prompt, parse_frontmatter as _parse_frontmatter, yaml_load)

logger = logging.getLogger("curator.skill_manager")


def tool_error(message: str, success: bool = False, **extra: Any) -> str:
    return json.dumps({"success": success, "error": message, **extra}, ensure_ascii=False)


def _skills_dir() -> Path:
    return paths.skills_dir()


MAX_NAME_LENGTH = 64
MAX_DESCRIPTION_LENGTH = 1024
MAX_SKILL_CONTENT_CHARS = 100_000
MAX_SKILL_FILE_BYTES = 1_048_576
VALID_NAME_RE = re.compile(r'^[a-z0-9][a-z0-9._-]*$')
ALLOWED_SUBDIRS = {"references", "templates", "scripts", "assets"}
_FRONTMATTER_END_RE = re.compile(r'\n---\s*\n')
_NAME_RULE = "Use lowercase letters, numbers, hyphens, dots, and underscores."


def _security_scan_skill(skill_dir: Path) -> Optional[str]:
    """No security scanner is shipped with the plugin (Hermes: ``skills.guard_agent_created``, default off)."""
    return None


# --- Validation helpers -------------------------------------------------------

def _check_identifier(value: str, label: str, invalid: str) -> Optional[str]:
    if len(value) > MAX_NAME_LENGTH:
        return f"{label} exceeds {MAX_NAME_LENGTH} characters."
    return None if VALID_NAME_RE.match(value) else invalid


def _validate_name(name: str) -> Optional[str]:
    if not name:
        return "Skill name is required."
    return _check_identifier(name, "Skill name", f"Invalid skill name '{name}'. {_NAME_RULE} Must start with a letter or digit.")


def _validate_category(category: Optional[str]) -> Optional[str]:
    if category is None or (isinstance(category, str) and not category.strip()):
        return None
    if not isinstance(category, str):
        return "Category must be a string."
    category = category.strip()
    invalid = f"Invalid category '{category}'. {_NAME_RULE} Categories must be a single directory name."
    if "/" in category or "\\" in category:
        return invalid
    return _check_identifier(category, "Category", invalid)


def _validate_frontmatter(content: str, *, new_skill: bool = False) -> Optional[str]:
    if not content.strip():
        return "Content cannot be empty."
    content = content.lstrip("﻿")
    if not content.startswith("---"):
        return "SKILL.md must start with YAML frontmatter (---). See existing skills for format."
    end_match = _FRONTMATTER_END_RE.search(content[3:])
    if not end_match:
        return "SKILL.md frontmatter is not closed. Ensure you have a closing '---' line."
    try:
        parsed = yaml_load(content[3:end_match.start() + 3])
    except Exception as e:
        return f"YAML frontmatter parse error: {e}"
    if not isinstance(parsed, dict):
        return "Frontmatter must be a YAML mapping (key: value pairs)."
    for field in ("name", "description"):
        if field not in parsed:
            return f"Frontmatter must include '{field}' field."
    desc = str(parsed["description"])
    if len(desc) > MAX_DESCRIPTION_LENGTH:
        return f"Description exceeds {MAX_DESCRIPTION_LENGTH} characters."
    if new_skill and len(desc.strip().strip("'\"")) > SKILL_PROMPT_DESC_LIMIT:
        return (f"Description is {len(desc.strip())} chars — new skills must fit the "
                f"{SKILL_PROMPT_DESC_LIMIT}-char system-prompt budget (one sentence, trigger first, "
                f"ends with a period). The skill index truncates longer descriptions to "
                f"{SKILL_PROMPT_DESC_LIMIT - 3} chars + '...', destroying the routing signal. "
                f"Move detail into the skill body.")
    if not content[end_match.end() + 3:].strip():
        return "SKILL.md must have content after the frontmatter (instructions, procedures, etc.)."
    return None


def _validate_content_size(content: str, label: str = "SKILL.md") -> Optional[str]:
    if len(content) > MAX_SKILL_CONTENT_CHARS:
        return (f"{label} content is {len(content):,} characters (limit: {MAX_SKILL_CONTENT_CHARS:,}). "
                f"Consider splitting into a smaller SKILL.md with supporting files in references/ or templates/.")
    return None


def _description_preview(content: str) -> str:
    with suppress(Exception):
        fm_end = _FRONTMATTER_END_RE.search(content[3:])
        if fm_end:
            return str(yaml_load(content[3:fm_end.start() + 3]).get("description", ""))[:120]
    return ""


def _resolve_skill_dir(name: str, category: Optional[str] = None) -> Path:
    base = _skills_dir()
    try:
        from curator.skill_utils import get_skill_create_dir
        base = get_skill_create_dir() or base
    except Exception:
        logger.debug("skills.create_dir lookup failed", exc_info=True)
    return base / (category or "") / name


def _iter_skill_dirs(root: Path):
    from curator.skill_utils import is_excluded_skill_path, rglob_following_symlinks
    for skill_md in rglob_following_symlinks(root, "SKILL.md"):
        if not is_excluded_skill_path(skill_md):
            yield skill_md.parent


def _find_skill(name: str) -> Optional[Dict[str, Any]]:
    """Find a skill (local dir, then create_dir/external) -> ``{"path": Path}`` | None. Accepts the bare
    dir name (matches nested skills too) and the categorized relative path (``mlops/axolotl``)."""
    from curator.skill_utils import get_all_skills_dirs
    local_root = None
    if "/" in name or "\\" in name:
        try:
            local_root = _skills_dir().resolve()
        except OSError:
            local_root = _skills_dir()
    for skills_dir in get_all_skills_dirs():
        if not skills_dir.exists():
            continue
        for skill_dir in _iter_skill_dirs(skills_dir):
            if skill_dir.name == name:
                return {"path": skill_dir}
            if local_root is not None:
                resolved = skill_dir.resolve()
                if resolved.is_relative_to(local_root) and resolved.relative_to(local_root).as_posix() == name:
                    return {"path": skill_dir}
    return None


def _skill_not_found_error(name: str, suffix: str = "") -> str:
    return f"Skill '{name}' not found. Use skills_list() to see available skills." + suffix


def _validate_file_path(file_path: str) -> Optional[str]:
    from curator.path_security import has_traversal_component
    if not file_path:
        return "file_path is required."
    parts = Path(file_path).parts
    if has_traversal_component(file_path):
        return "Path traversal ('..') is not allowed."
    if parts and parts[-1] == "SKILL.md" and len(parts) in (1, 2):
        return None
    if not parts or parts[0] not in ALLOWED_SUBDIRS:
        return f"File must be under one of: {', '.join(sorted(ALLOWED_SUBDIRS))}. Got: '{file_path}'"
    if len(parts) < 2:
        return f"Provide a file path, not just a directory. Example: '{parts[0]}/myfile.md'"
    return None


def _resolve_supporting_file(skill_dir: Path, file_path: str):
    from curator.path_security import validate_within_dir
    target = skill_dir / (file_path or "")
    err = _validate_file_path(file_path) or validate_within_dir(target, skill_dir)
    return (None, _err(err)) if err else (target, None)


def _locate_for_write(name: str, action: str, not_found_suffix: str = "", *, org_guard: bool = True):
    existing = _find_skill(name)
    if not existing:
        return None, _err(_skill_not_found_error(name, not_found_suffix))
    skill_dir = existing["path"]
    guard = ((org_guard and _org_mirror_write_guard(name, skill_dir, action))
             or _background_review_write_guard(name, skill_dir, action))
    return (None, guard) if guard else (skill_dir, None)


def _guarded_write(name: str, skill_dir: Path, target: Path, action: str, label: str, content: str) -> Optional[Dict[str, Any]]:
    original = None
    if target.exists():
        if read_guard := _background_review_read_before_write_guard(name, target, action, label):
            return read_guard
        original = target.read_text(encoding="utf-8")
    target.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(target, content, preserve_mode=True, create_mode=0o644)
    scan_error = _security_scan_skill(skill_dir)
    if not scan_error:
        return None
    if original is not None:
        atomic_write_text(target, original, preserve_mode=True)
    else:
        target.unlink(missing_ok=True)
    return _err(scan_error)


def _attach_org_note(result: Dict[str, Any], name: str, skill_dir: Path) -> Dict[str, Any]:
    if org_note := _maybe_auto_propose_org_edit(name, skill_dir):
        result["org_sharing"] = org_note
        result["message"] = f"{result['message']} {org_note}"
    return result


def _add_description_prompt_preview(result: Dict[str, Any], content: str) -> Dict[str, Any]:
    fm, _ = _parse_frontmatter(content)
    if is_skill_description_truncated_for_prompt(fm):
        result["system_prompt_preview"] = (f"System prompt will show: \"{extract_skill_description(fm)}\" — keep the trigger "
                                           f"self-contained in the first {SKILL_PROMPT_DESC_LIMIT - 3} chars.")
    return result


def _clip(text: str, n: int, ellipsis: str) -> str:
    return text[:n] + (ellipsis if len(text) > n else "")


# --- Core actions -------------------------------------------------------------

def _create_skill(name: str, content: str, category: Optional[str] = None) -> Dict[str, Any]:
    if err := (_validate_name(name) or _validate_category(category)
               or _validate_frontmatter(content, new_skill=True) or _validate_content_size(content)):
        return _err(err)
    if existing := _find_skill(name):
        return _err(f"A skill named '{name}' already exists at {existing['path']}.")
    skill_dir = _resolve_skill_dir(name, category)
    skill_dir.mkdir(parents=True, exist_ok=True)
    skill_md = skill_dir / "SKILL.md"
    atomic_write_text(skill_md, content, preserve_mode=True, create_mode=0o644)
    if scan_error := _security_scan_skill(skill_dir):
        shutil.rmtree(skill_dir, ignore_errors=True)
        return _err(scan_error)
    root = _skills_dir()
    display = skill_dir.relative_to(root) if skill_dir.is_relative_to(root) else skill_dir
    result = {"success": True, "message": f"Skill '{name}' created.", "path": str(display), "skill_md": str(skill_md),
              "_change": {"description": _description_preview(content)},
              **({"category": category} if category else {}),
              "hint": "To add reference files, templates, or scripts, use "
                      f"skill_manage(action='write_file', name='{name}', file_path='references/example.md', file_content='...')"}
    return _add_description_prompt_preview(result, content)


def _edit_skill(name: str, content: str) -> Dict[str, Any]:
    if err := _validate_frontmatter(content) or _validate_content_size(content):
        return _err(err)
    skill_dir, guard = _locate_for_write(name, "edit")
    if guard := guard or _guarded_write(name, skill_dir, skill_dir / "SKILL.md", "edit", "SKILL.md", content):
        return guard
    result = {"success": True, "message": f"Skill '{name}' updated (full rewrite).", "path": str(skill_dir),
              "_change": {"description": _description_preview(content)}}
    return _add_description_prompt_preview(_attach_org_note(result, name, skill_dir), content)


def _patch_skill(name: str, old_string: str, new_string: Optional[str], file_path: Optional[str] = None,
                 replace_all: bool = False) -> Dict[str, Any]:
    if not old_string:
        return _err("old_string is required for 'patch' and must be the EXACT text currently in the file. "
                    "Read the target file first (skill_view on the skill's SKILL.md, or the file named by "
                    "file_path) and copy the snippet verbatim, then retry 'patch'. Do NOT fall back to "
                    "action='write_file' — that rewrites the entire file and destroys unrelated content.")
    if new_string is None:
        return _err("new_string is required for 'patch'. Use an empty string to delete matched text.")
    skill_dir, guard = _locate_for_write(name, "patch")
    if guard:
        return guard
    target_label = file_path or "SKILL.md"
    if file_path:
        target, err = _resolve_supporting_file(skill_dir, file_path)
        if err:
            return err
    else:
        target = skill_dir / "SKILL.md"
    if not target.exists():
        return _err(f"File not found: {target.relative_to(skill_dir)}")
    if read_guard := _background_review_read_before_write_guard(name, target, "patch", target_label):
        return read_guard
    content = target.read_text(encoding="utf-8")
    from curator.fuzzy_match import format_no_match_hint, fuzzy_find_and_replace
    new_content, match_count, _strategy, match_error = fuzzy_find_and_replace(content, old_string, new_string, replace_all)
    if match_error:
        with suppress(Exception):
            match_error += format_no_match_hint(match_error, match_count, old_string, content)
        return {**_err(match_error), "file_preview": _clip(content, 500, "...")}
    if err := _validate_content_size(new_content, label=target_label):
        return _err(err)
    if not file_path and (err := _validate_frontmatter(new_content)):
        return _err(f"Patch would break SKILL.md structure: {err}")
    if guard := _guarded_write(name, skill_dir, target, "patch", target_label, new_content):
        return guard
    result = {"success": True,
              "message": f"Patched {target_label} in skill '{name}' ({match_count} replacement{'s' if match_count > 1 else ''}).",
              "_change": {"old": _clip(old_string, 200, "…"), "new": _clip(new_string, 200, "…")}}
    return _attach_org_note(result, name, skill_dir)


def _delete_skill(name: str, absorbed_into: Optional[str] = None) -> Dict[str, Any]:
    """Delete a skill. ``absorbed_into``: None = undeclared (legacy, accepted); "" = explicit prune;
    "<skill>" = absorbed into that umbrella, which must exist. Curator-pass deletes ARCHIVE instead."""
    skill_dir, guard = _locate_for_write(name, "delete")
    if guard := guard or _curator_consolidation_delete_guard(name, absorbed_into):
        return guard
    if pinned_err := _pinned_guard(name):
        return _err(pinned_err)
    absorbed_target = absorbed_into.strip() if isinstance(absorbed_into, str) else ""
    if absorbed_target:
        if absorbed_target == name:
            return _err(f"absorbed_into='{absorbed_target}' cannot equal the skill being deleted.")
        if not _find_skill(absorbed_target):
            return _err(f"absorbed_into='{absorbed_target}' does not exist. "
                        f"Create or patch the umbrella skill first, then retry the delete.")
    skills_root = _containing_skills_root(skill_dir)
    if unsafe := _validate_delete_target(skill_dir):
        return _err(unsafe)
    absorbed_note = f" Content absorbed into '{absorbed_target}'." if absorbed_target else ""
    if _is_background_review():
        try:
            from curator.skill_usage import archive_skill
            ok, archive_msg = archive_skill(name)
        except Exception as e:
            return _err(f"failed to archive '{name}': {e}")
        if not ok:
            return _err(archive_msg)
        return {"success": True, "message": f"Skill '{name}' archived ({archive_msg}).{absorbed_note}", "_archived": True}
    shutil.rmtree(skill_dir)
    _rmdir_if_empty(skill_dir.parent, skills_root)
    return {"success": True, "message": f"Skill '{name}' deleted.{absorbed_note}"}


def _rmdir_if_empty(parent: Path, stop: Path) -> None:
    if parent != stop and parent.exists() and not any(parent.iterdir()):
        parent.rmdir()


def _write_file(name: str, file_path: str, file_content: Optional[str]) -> Dict[str, Any]:
    if err := _validate_file_path(file_path):
        return _err(err)
    if not file_content and file_content != "":
        return _err("file_content is required.")
    if (content_bytes := len(file_content.encode("utf-8"))) > MAX_SKILL_FILE_BYTES:
        return _err(f"File content is {content_bytes:,} bytes (limit: {MAX_SKILL_FILE_BYTES:,} bytes / 1 MiB). "
                    f"Consider splitting into smaller files.")
    if err := _validate_content_size(file_content, label=file_path):
        return _err(err)
    skill_dir, guard = _locate_for_write(name, "write_file", " Create it first with action='create'.")
    if guard:
        return guard
    target, err = _resolve_supporting_file(skill_dir, file_path)
    if guard := err or _guarded_write(name, skill_dir, target, "write_file", file_path, file_content):
        return guard
    return _attach_org_note({"success": True, "message": f"File '{file_path}' written to skill '{name}'.", "path": str(target)},
                            name, skill_dir)


def _remove_file(name: str, file_path: str) -> Dict[str, Any]:
    if err := _validate_file_path(file_path):
        return _err(err)
    skill_dir, guard = _locate_for_write(name, "remove_file", org_guard=False)
    if guard:
        return guard
    target, err = _resolve_supporting_file(skill_dir, file_path)
    if err:
        return err
    if not target.exists():
        available = [str(f.relative_to(skill_dir)) for subdir in sorted(ALLOWED_SUBDIRS)
                     if (skill_dir / subdir).exists() for f in sorted((skill_dir / subdir).rglob("*")) if f.is_file()]
        return _err(f"File '{file_path}' not found in skill '{name}'.", available_files=available or None)
    if read_guard := _background_review_read_before_write_guard(name, target, "remove_file", file_path):
        return read_guard
    target.unlink()
    _rmdir_if_empty(target.parent, skill_dir)
    return {"success": True, "message": f"File '{file_path}' removed from skill '{name}'."}


# --- Main entry point ---------------------------------------------------------

_skill_gate_bypass: _ctxvars.ContextVar = _ctxvars.ContextVar("skill_gate_bypass", default=False)


def _run_write_gate(build_staging):
    """No staged write-approval gate in the plugin: always proceed."""
    return None


def _apply_skill_write_gate(action, name, **payload_kwargs):
    if action not in _ACTION_HANDLERS or _skill_gate_bypass.get():
        return None
    return _run_write_gate(lambda wa: None)


_FLAT_OP_KEYS = ("content", "category", "file_path", "file_content", "old_string", "new_string", "absorbed_into", "operations")


def _skill_manage_from(payload: Dict[str, Any], **extra) -> str:
    return skill_manage(action=payload.get("action", ""), name=payload.get("name", ""),
                        replace_all=payload.get("replace_all", False),
                        **{k: payload.get(k) for k in _FLAT_OP_KEYS}, **extra)


def _act_patch(a):
    if a["content"] and (a["old_string"] or a["new_string"] is not None):
        return tool_error("Pass EITHER content (full SKILL.md rewrite) OR old_string/new_string (targeted replacement), not both.",
                          success=False)
    if a["content"]:
        return _edit_skill(a["name"], a["content"])
    return _patch_skill(a["name"], a["old_string"], a["new_string"], a["file_path"], a["replace_all"])


_ACTION_HANDLERS = {
    "create": lambda a: _create_skill(a["name"], a["content"], a["category"]),
    "edit": lambda a: _edit_skill(a["name"], a["content"]),
    "patch": _act_patch,
    "delete": lambda a: _delete_skill(a["name"], absorbed_into=a["absorbed_into"]),
    "write_file": lambda a: _write_file(a["name"], a["file_path"], a["file_content"]),
    "remove_file": lambda a: _remove_file(a["name"], a["file_path"])}
_MISSING, _IS_NONE = (lambda v: not v), (lambda v: v is None)
_REQUIRED_ARGS = {
    "create": [("content", _MISSING, "content is required for 'create'. Provide the full SKILL.md text (frontmatter + body).")],
    "edit": [("content", _MISSING, "content is required for a full rewrite. Provide the full updated SKILL.md text.")],
    "write_file": [("file_path", _MISSING, "file_path is required for 'write_file'. Example: 'references/api-guide.md'"),
                   ("file_content", _IS_NONE, "file_content is required for 'write_file'.")],
    "remove_file": [("file_path", _MISSING, "file_path is required for 'remove_file'.")]}


def _record_success(action, name, result, *, file_path, absorbed_into, task_id, session_id, ledger_before) -> None:
    """Best-effort post-mutation side effects (never break the tool): ledger + curator telemetry."""
    with suppress(Exception):
        from curator import skill_ledger as _ledger
        _post = _find_skill(name)
        _evidence = ({"absorbed_into": absorbed_into, "archived": bool(result.get("_archived"))} if action == "delete" else {})
        _evidence.update({k: v for k, v in (("session_id", session_id), ("file_path", file_path)) if v})
        _ledger.record_mutation(action, name, before=ledger_before if ledger_before is not None else [],
                                after_root=_post["path"] if _post else None, evidence=_evidence)
    with suppress(Exception):
        from curator.skill_provenance import is_background_review
        from curator.skill_usage import bump_patch, forget, record_created
        if action == "create":
            record_created(name, agent_created=is_background_review(), task_id=task_id, session_id=session_id)
        elif action in {"patch", "edit", "write_file", "remove_file"}:
            bump_patch(name, action=action, task_id=task_id, session_id=session_id)
        elif action == "delete" and not result.get("_archived"):
            forget(name)


def skill_manage(action: str, name: str, content=None, category=None, file_path=None, file_content=None,
                 old_string=None, new_string=None, replace_all: bool = False, absorbed_into=None,
                 task_id=None, session_id=None, operations=None) -> str:
    """Dispatch to the action handler -> JSON string. ``operations`` (atomic batch shape) overrides the flat fields."""
    if operations is not None:
        return _skill_manage_batch(operations, default_name=name or None, task_id=task_id, session_id=session_id)
    if (preflight := _background_review_preflight(action, name)) is not None:
        return json.dumps(preflight, ensure_ascii=False)
    args = dict(content=content, category=category, file_path=file_path, file_content=file_content,
                old_string=old_string, new_string=new_string, replace_all=replace_all, absorbed_into=absorbed_into)
    if (gate_result := _apply_skill_write_gate(action, name, **args)) is not None:
        return gate_result
    _ledger_before = None
    with suppress(Exception):
        from curator import skill_ledger as _ledger
        _pre = _find_skill(name)
        _ledger_before = _ledger.capture_before(_pre["path"] if _pre else None, complete_package=(action == "delete"), skill=name)
    for arg, missing, message in _REQUIRED_ARGS.get(action, ()):
        if missing(args[arg]):
            return tool_error(message, success=False)
    handler = _ACTION_HANDLERS.get(action, lambda a: _err(
        f"Unknown action '{action}'. Use: create, edit, patch, delete, write_file, remove_file"))
    result = handler({"name": name, **args})
    if isinstance(result, str):
        return result
    if result.get("success"):
        _record_success(action, name, result, file_path=file_path, absorbed_into=absorbed_into,
                        task_id=task_id, session_id=session_id, ledger_before=_ledger_before)
    return json.dumps(result, ensure_ascii=False)


# --- Tool schema (OpenAI function-calling shape, as advertised by Hermes) -------

SKILL_MANAGE_SCHEMA = {
    "name": "skill_manage",
    "description": (
        "Create, update, or delete skills — your procedural memory for recurring task types. The call is an "
        "operations array (a single edit is a list of one); it applies atomically — any failure rolls every "
        f"touched skill back. Ops: create (full SKILL.md; lands in {display_skill_create_dir()}; must precede that "
        "skill's other ops), patch (targeted old_string/new_string fix — preferred; content alone REPLACES the whole "
        "file, read it via skill_view() first), write_file/remove_file (supporting files), delete (sole op only). "
        "Existing skills are modified wherever they live. Keep the description's first 57 chars a self-contained "
        "trigger: 'Use when <trigger>. <one-line behavior>.' Write lessons, not logs: imperative rule + why, no PR "
        "numbers/dates/incident narration, one rule per lesson, references/ named by topic (extend before adding). "
        "skill_view() shows format conventions."),
    "parameters": {
        "type": "object",
        "properties": {
            "operations": {
                "type": "array",
                "description": "Ordered ops; each names its target skill.",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string", "description": "Skill name (lowercase, hyphens/underscores, max 64 chars); an existing skill's name unless creating."},
                        "action": {"type": "string", "enum": ["create", "patch", "delete", "write_file", "remove_file"]},
                        "content": {"type": "string", "description": "Full SKILL.md text (YAML frontmatter + markdown body) for create, or a full rewrite on patch."},
                        "category": {"type": "string", "description": "Optional category subdir for create (e.g. 'devops')."},
                        "old_string": {"type": "string", "description": "Text to find (patch; same matching semantics as the patch tool)."},
                        "new_string": {"type": "string", "description": "Replacement (patch); empty string deletes the match."},
                        "replace_all": {"type": "boolean", "description": "patch: replace all occurrences (default false)."},
                        "file_path": {"type": "string", "description": "Path RELATIVE to the skill's own directory, e.g. 'references/api.md'. write_file/remove_file: required; first segment references/, templates/, scripts/, or assets/. patch: optional (default SKILL.md)."},
                        "file_content": {"type": "string", "description": "Content for write_file."},
                        "absorbed_into": {"type": "string", "description": "delete only: the umbrella that absorbed this skill (\"\" = explicit prune)."},
                    },
                    "required": ["name", "action"],
                },
            },
        },
        "required": ["operations"],
    },
}
