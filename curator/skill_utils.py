"""Skill tree helpers — the slice of ``agent/skill_utils.py`` the curator needs.

Scanner rules, frontmatter parsing, skills-dir enumeration (local / create_dir /
external / trusted project), external-ownership checks, lookup-name
normalisation, platform gating and description truncation.

YAML: Hermes uses PyYAML. Without a dependency this module ships a small
block-YAML reader (``_MiniYaml``) covering what SKILL.md frontmatter actually
uses — nested mappings, block + flow sequences, quoted/plain scalars, comments.
PyYAML is used when importable; a parse failure falls back to Hermes's own
``key: value`` line split, exactly as upstream.
"""

from __future__ import annotations

import logging
import os
import re
import sys
from pathlib import Path, PurePath
from typing import Any, Dict, List, Optional, Set, Tuple

from curator import paths

logger = logging.getLogger(__name__)

PLATFORM_MAP = {"macos": "darwin", "linux": "linux", "windows": "win32"}

EXCLUDED_SKILL_DIRS = frozenset((
    ".git", ".github", ".hub", ".archive", ".curator_backups",
    ".venv", "venv", "node_modules", "site-packages", "__pycache__",
    ".tox", ".nox", ".pytest_cache", ".mypy_cache", ".ruff_cache",
))
SKILL_SUPPORT_DIRS = frozenset(("references", "templates", "assets", "scripts"))

ORG_MIRROR_DIR_NAME = "_org"
ORG_ACTIVE_MARKER = ".active_org"

# Permanently pinned by the system prompt in Hermes; kept for message parity.
ESSENTIAL_SKILLS: frozenset = frozenset({"hermes-agent"})

SKILL_PROMPT_DESC_LIMIT = 60
PROJECT_SKILLS_SUBDIRS = (os.path.join(".hermes", "skills"), os.path.join(".agents", "skills"))
_PROJECT_ROOT_MAX_DEPTH = 64


# --- org mirror ---------------------------------------------------------------

def read_active_org_id(skills_dir: Path) -> Optional[str]:
    marker = skills_dir / ORG_MIRROR_DIR_NAME / ORG_ACTIVE_MARKER
    try:
        return (marker.read_text(encoding="utf-8").strip() or None) if marker.exists() else None
    except OSError:
        return None


def _org_rel_parts(path, skills_dir: Path) -> Tuple[str, ...]:
    try:
        parts = Path(path).resolve().relative_to(Path(skills_dir).resolve()).parts
    except (OSError, ValueError):
        return ()
    return parts if parts and parts[0] == ORG_MIRROR_DIR_NAME else ()


def is_org_mirror_path(path, skills_dir: Path) -> bool:
    return bool(_org_rel_parts(path, skills_dir))


def rglob_following_symlinks(base: Path, pattern: str):
    """``base.rglob(pattern)`` that follows symlinked directories on every Python version.

    ``Path.rglob``'s ``**`` never descends into symlinked directories before 3.13 and only does so
    with ``recurse_symlinks=True`` from 3.13, so it disagreed with ``iter_skill_index_files``
    (``os.walk(followlinks=True)``) for a symlinked skill dir — e.g.
    ``~/.claude/skills/x -> ../../.agents/skills/x``. Built on ``os.walk`` for one behaviour everywhere;
    *pattern* is a filename glob matched against basenames (sorted, deterministic)."""
    import fnmatch
    for root, _dirs, files in os.walk(str(base), followlinks=True):
        for name in sorted(files):
            if fnmatch.fnmatch(name, pattern):
                yield Path(root) / name


# --- scanner exclusions -------------------------------------------------------

def is_skill_support_path(path, *, root: Optional[Path] = None) -> bool:
    """True when *path* sits under a ``references/templates/assets/scripts`` dir
    of a skill root (a dir holding SKILL.md), so a nested SKILL.md there is a
    support file, not a skill."""
    p = Path(path)
    parents = list(p.parents)
    for parent in parents:
        if root is not None:
            try:
                parent.relative_to(root)
            except ValueError:
                break
        if parent.name in SKILL_SUPPORT_DIRS and (parent.parent / "SKILL.md").exists():
            return True
    return False


def is_excluded_skill_path(path, *, root: Optional[Path] = None) -> bool:
    parts = PurePath(str(path)).parts
    return any(part in EXCLUDED_SKILL_DIRS for part in parts) or is_skill_support_path(path, root=root)


# --- YAML ---------------------------------------------------------------------

class _MiniYamlError(ValueError):
    pass


class _MiniYaml:
    """Block-YAML subset reader: mappings, sequences, scalars, flow lists, comments."""

    _SCALAR_TRUE = {"true", "yes", "on"}
    _SCALAR_FALSE = {"false", "no", "off"}
    _SCALAR_NULL = {"null", "~", ""}

    def __init__(self, text: str) -> None:
        self.lines: List[Tuple[int, str]] = []
        for raw in text.splitlines():
            stripped = self._strip_comment(raw)
            if not stripped.strip():
                continue
            indent = len(stripped) - len(stripped.lstrip(" "))
            if "\t" in stripped[:indent + 1]:
                raise _MiniYamlError("tabs are not allowed for indentation")
            self.lines.append((indent, stripped.strip()))
        self.pos = 0

    @staticmethod
    def _strip_comment(line: str) -> str:
        out, quote = [], None
        for i, ch in enumerate(line):
            if quote:
                out.append(ch)
                if ch == quote:
                    quote = None
            elif ch in "\"'":
                quote = ch
                out.append(ch)
            elif ch == "#" and (i == 0 or line[i - 1] in " \t"):
                break
            else:
                out.append(ch)
        return "".join(out).rstrip()

    def load(self) -> Any:
        if not self.lines:
            return None
        value = self._parse_block(self.lines[0][0])
        if self.pos != len(self.lines):
            raise _MiniYamlError("trailing content")
        return value

    def _parse_block(self, indent: int) -> Any:
        _, text = self.lines[self.pos]
        if text.startswith("- ") or text == "-":
            return self._parse_seq(indent)
        return self._parse_map(indent)

    def _parse_seq(self, indent: int) -> List[Any]:
        out: List[Any] = []
        while self.pos < len(self.lines):
            ind, text = self.lines[self.pos]
            if ind < indent:
                break
            if ind > indent or not (text.startswith("- ") or text == "-"):
                raise _MiniYamlError(f"bad sequence item: {text!r}")
            item = text[1:].strip()
            self.pos += 1
            if not item:
                out.append(self._parse_child(indent))
            elif self._looks_like_key(item):
                # "- key: value" — a mapping whose first key is inline.
                key, _, rest = item.partition(":")
                mapping: Dict[str, Any] = {}
                mapping[key.strip()] = self._inline_value(rest.strip(), indent + 2)
                child_indent = self._next_indent()
                if child_indent is not None and child_indent > indent:
                    mapping.update(self._parse_map(child_indent))
                out.append(mapping)
            else:
                out.append(self._scalar(item))
        return out

    def _parse_map(self, indent: int) -> Dict[str, Any]:
        out: Dict[str, Any] = {}
        while self.pos < len(self.lines):
            ind, text = self.lines[self.pos]
            if ind < indent:
                break
            if ind > indent:
                raise _MiniYamlError(f"unexpected indent: {text!r}")
            if not self._looks_like_key(text):
                raise _MiniYamlError(f"expected 'key: value', got {text!r}")
            key, _, rest = text.partition(":")
            self.pos += 1
            out[key.strip().strip("\"'")] = self._inline_value(rest.strip(), indent)
        return out

    def _inline_value(self, rest: str, indent: int) -> Any:
        if rest == "" or rest in ("|", ">"):
            child = self._next_indent()
            if rest in ("|", ">"):
                return self._block_scalar(indent, fold=rest == ">")
            if child is not None and child > indent:
                return self._parse_child(indent)
            if child is not None and child == indent and self._next_is_seq():
                return self._parse_seq(indent)
            return None
        return self._scalar(rest)

    def _block_scalar(self, indent: int, *, fold: bool) -> str:
        collected: List[str] = []
        while self.pos < len(self.lines):
            ind, text = self.lines[self.pos]
            if ind <= indent:
                break
            collected.append(text)
            self.pos += 1
        return (" " if fold else "\n").join(collected)

    def _next_indent(self) -> Optional[int]:
        return self.lines[self.pos][0] if self.pos < len(self.lines) else None

    def _next_is_seq(self) -> bool:
        return self.pos < len(self.lines) and (self.lines[self.pos][1].startswith("- ") or self.lines[self.pos][1] == "-")

    def _parse_child(self, parent_indent: int) -> Any:
        child_indent = self._next_indent()
        if child_indent is None or child_indent <= parent_indent:
            return None
        return self._parse_block(child_indent)

    @staticmethod
    def _looks_like_key(text: str) -> bool:
        if text.startswith(("[", "{", "\"", "'")) and ":" not in text.split(" ", 1)[0]:
            m = re.match(r"^(\"[^\"]*\"|'[^']*')\s*:", text)
            return bool(m)
        m = re.match(r"^[^\s:][^:]*?:(\s|$)", text)
        return bool(m)

    def _scalar(self, text: str) -> Any:
        text = text.strip()
        if text.startswith("[") and text.endswith("]"):
            inner = text[1:-1].strip()
            return [self._scalar(x) for x in self._split_flow(inner)] if inner else []
        if text.startswith("{") and text.endswith("}"):
            inner = text[1:-1].strip()
            result: Dict[str, Any] = {}
            for part in self._split_flow(inner) if inner else []:
                k, _, v = part.partition(":")
                result[k.strip().strip("\"'")] = self._scalar(v.strip())
            return result
        if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
            body = text[1:-1]
            return body.replace('\\"', '"').replace("\\n", "\n") if text[0] == '"' else body.replace("''", "'")
        if text.startswith(("[", "{", "\"", "'")):
            raise _MiniYamlError(f"unterminated scalar: {text!r}")
        low = text.lower()
        if low in self._SCALAR_TRUE:
            return True
        if low in self._SCALAR_FALSE:
            return False
        if low in self._SCALAR_NULL:
            return None
        if re.fullmatch(r"[-+]?\d+", text):
            return int(text)
        if re.fullmatch(r"[-+]?(\d+\.\d*|\.\d+|\d+)([eE][-+]?\d+)?", text):
            try:
                return float(text)
            except ValueError:
                pass
        return text

    @staticmethod
    def _split_flow(inner: str) -> List[str]:
        parts, depth, quote, cur = [], 0, None, []
        for ch in inner:
            if quote:
                cur.append(ch)
                if ch == quote:
                    quote = None
            elif ch in "\"'":
                quote = ch
                cur.append(ch)
            elif ch in "[{":
                depth += 1
                cur.append(ch)
            elif ch in "]}":
                depth -= 1
                cur.append(ch)
            elif ch == "," and depth == 0:
                parts.append("".join(cur).strip())
                cur = []
            else:
                cur.append(ch)
        if "".join(cur).strip():
            parts.append("".join(cur).strip())
        return parts


def yaml_load(content: str) -> Any:
    """PyYAML when available, else the built-in subset reader. Raises on malformed input."""
    try:
        import yaml  # type: ignore
    except ImportError:
        return _MiniYaml(content).load()
    return yaml.load(content, Loader=getattr(yaml, "CSafeLoader", None) or yaml.SafeLoader)


def parse_frontmatter(content: str) -> Tuple[Dict[str, Any], str]:
    """(frontmatter dict, body). Malformed YAML falls back to ``key: value`` line splitting.
    A leading UTF-8 BOM is stripped first or it would defeat the ``---`` fence check."""
    content = content.lstrip("﻿")
    end_match = re.search(r"\n---\s*\n", content[3:]) if content.startswith("---") else None
    if not end_match:
        return {}, content
    yaml_content = content[3: end_match.start() + 3]
    body = content[end_match.end() + 3:]
    frontmatter: Dict[str, Any] = {}
    try:
        parsed = yaml_load(yaml_content)
        if isinstance(parsed, dict):
            frontmatter = parsed
    except Exception:
        for line in yaml_content.strip().split("\n"):
            if ":" in line:
                key, value = line.split(":", 1)
                frontmatter[key.strip()] = value.strip()
    return frontmatter, body


# --- platform / disabled ------------------------------------------------------

def skill_matches_platform_list(platforms: Any) -> bool:
    if not platforms:
        return True
    if isinstance(platforms, str):
        platforms = [p.strip() for p in platforms.split(",")]
    wanted = {PLATFORM_MAP.get(str(p).lower(), str(p).lower()) for p in platforms if p}
    return not wanted or sys.platform in wanted or any(sys.platform.startswith(w) for w in wanted)


def skill_matches_platform(frontmatter: Dict[str, Any]) -> bool:
    return skill_matches_platform_list(frontmatter.get("platforms") if isinstance(frontmatter, dict) else None)


def parse_config_string_list(value) -> List[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [v.strip() for v in value.split(",") if v.strip()]
    if isinstance(value, (list, tuple, set)):
        return [str(v).strip() for v in value if str(v).strip()]
    return []


def get_disabled_skill_names(platform: Optional[str] = None) -> Set[str]:
    from curator.config import cfg_get, load_config_readonly
    skills_cfg = load_config_readonly().get("skills") or {}
    names = set(parse_config_string_list(skills_cfg.get("disabled")))
    resolved = platform or os.environ.get("HERMES_PLATFORM") or ""
    if resolved:
        names |= set(parse_config_string_list(cfg_get(skills_cfg, "platform_disabled", resolved)))
    return names


# --- skills dirs --------------------------------------------------------------

def get_skills_dir() -> Path:
    return paths.skills_dir()


def _config_str_list(raw) -> List[str]:
    return parse_config_string_list(raw)


def get_external_skills_dirs() -> List[Path]:
    """Validated, deduplicated ``skills.external_dirs`` (existing dirs only)."""
    from curator.config import load_config_readonly
    skills_cfg = load_config_readonly().get("skills") or {}
    local = get_skills_dir().resolve()
    result: List[Path] = []
    for entry in _config_str_list(skills_cfg.get("external_dirs")):
        p = paths.expand_path(entry).resolve()
        if p == local or p in result:
            continue
        if p.is_dir():
            result.append(p)
        else:
            logger.debug("External skills dir does not exist, skipping: %s", p)
    return result


def get_skill_create_dir() -> Optional[Path]:
    from curator.config import load_config_readonly
    raw = (load_config_readonly().get("skills") or {}).get("create_dir") or ""
    if not isinstance(raw, str) or not raw.strip():
        return None
    return paths.expand_path(raw.strip()).resolve()


def display_skill_create_dir() -> str:
    create = get_skill_create_dir()
    return str(create) if create else f"{paths.display_home()}/skills/"


def get_all_skills_dirs() -> List[Path]:
    """Local first, then create_dir, then external. Trusted project dirs are NOT included."""
    dirs = [get_skills_dir()]
    create_dir = get_skill_create_dir()
    if create_dir is not None and create_dir.is_dir():
        dirs.append(create_dir)
    dirs.extend(d for d in get_external_skills_dirs() if d not in dirs)
    return dirs


def find_project_root(start: Optional[Path] = None) -> Optional[Path]:
    cur = (start or Path.cwd()).resolve()
    for _ in range(_PROJECT_ROOT_MAX_DEPTH):
        if (cur / ".git").exists():
            return cur
        if cur.parent == cur:
            return None
        cur = cur.parent
    return None


def _project_trusted_dirs_from_config() -> Set[Path]:
    from curator.config import load_config_readonly
    raw = (load_config_readonly().get("skills") or {}).get("trusted_project_dirs") or []
    return {paths.expand_path(e).resolve() for e in _config_str_list(raw)}


def is_project_root_trusted(root: Path) -> bool:
    try:
        return root.resolve() in _project_trusted_dirs_from_config()
    except OSError:
        return False


def get_project_skills_dirs() -> List[Path]:
    from curator.config import load_config_readonly
    if not (load_config_readonly().get("skills") or {}).get("project_discovery", True):
        return []
    root = find_project_root()
    if root is None or not is_project_root_trusted(root):
        return []
    return [root / sub for sub in PROJECT_SKILLS_SUBDIRS if (root / sub).is_dir()]


def _resolve_for_skill_ownership(path) -> Path:
    path_obj = path if isinstance(path, Path) else Path(str(path))
    try:
        return path_obj.expanduser().resolve()
    except (OSError, RuntimeError):
        return path_obj.expanduser().absolute()


def is_external_skill_path(path) -> bool:
    """Under an external or trusted-project skills dir: externally owned, read-only to curation."""
    candidate = _resolve_for_skill_ownership(path)
    roots: List[Path] = list(get_external_skills_dirs())
    try:
        roots.extend(get_project_skills_dirs())
    except Exception:
        pass
    return any(candidate.is_relative_to(_resolve_for_skill_ownership(root)) for root in roots)


def iter_skill_index_files(skills_dir: Path, filename: str):
    """Sorted paths matching *filename*; prunes EXCLUDED_SKILL_DIRS and support dirs of
    skill roots. ``_org/`` is TOKEN-GATED on ``.active_org``."""
    skills_dir_str = str(skills_dir)
    active_org = read_active_org_id(skills_dir)
    org_root = os.path.join(skills_dir_str, ORG_MIRROR_DIR_NAME)
    matches: List[str] = []
    for root, dirs, files in os.walk(skills_dir_str, followlinks=True):
        has_skill_md = "SKILL.md" in files
        if root == skills_dir_str and ORG_MIRROR_DIR_NAME in dirs and active_org is None:
            dirs.remove(ORG_MIRROR_DIR_NAME)
        elif root == org_root:
            dirs[:] = [d for d in dirs if d == active_org]
        dirs[:] = [d for d in dirs if d not in EXCLUDED_SKILL_DIRS and not (has_skill_md and d in SKILL_SUPPORT_DIRS)]
        if filename in files:
            matches.append(os.path.join(root, filename))
    yield from map(Path, sorted(matches))


def normalize_skill_lookup_name(identifier: str) -> str:
    """Absolute skill path under a trusted root -> relative form ``skill_view`` accepts."""
    raw_identifier = (identifier or "").strip()
    if not raw_identifier:
        return raw_identifier
    identifier_path = Path(raw_identifier).expanduser()
    if not identifier_path.is_absolute():
        return raw_identifier.lstrip("/")
    primary_root = get_skills_dir()
    trusted_roots = [primary_root]
    for getter in (get_project_skills_dirs, get_external_skills_dirs):
        try:
            trusted_roots.extend(getter())
        except Exception:
            pass
    for root in trusted_roots:
        if identifier_path.is_relative_to(root):
            return str(identifier_path.relative_to(root))
    try:
        return str(identifier_path.resolve().relative_to(primary_root.resolve()))
    except Exception:
        return raw_identifier


# --- description helpers ------------------------------------------------------

def _normalize_skill_description(frontmatter: Dict[str, Any]) -> str:
    return " ".join(str(frontmatter.get("description", "") or "").split())


def extract_skill_description(frontmatter: Dict[str, Any]) -> str:
    desc = _normalize_skill_description(frontmatter)
    return desc[:SKILL_PROMPT_DESC_LIMIT - 3] + "..." if len(desc) > SKILL_PROMPT_DESC_LIMIT else desc


def is_skill_description_truncated_for_prompt(frontmatter: Dict[str, Any]) -> bool:
    return len(_normalize_skill_description(frontmatter)) > SKILL_PROMPT_DESC_LIMIT
