"""Skills TUI — enable/disable, browse, edit (``curator skills``; Herdr pane ``skills``).

A checklist where *selected* means *enabled*,
persisted to ``skills.disabled`` (or ``skills.platform_disabled.<platform>``),
with essential skills never disableable and whole categories toggleable.

Plugin additions on top of that checklist: the same screen shows curator
state (managed / pinned / stale / archived, usage counters), and offers the
lifecycle verbs (pin, adopt, archive, restore), a pager (``Enter``) and
``$EDITOR`` on ``SKILL.md`` (``e``). An edit that changes the file is ledgered
as ``actor=user`` and counted as a patch, so hand edits are rollback-able too.

Structure: ``SkillsModel`` (data + actions) and ``Controller`` (key handling,
text rendering) are curses-free and fully tested; ``run_tui`` is a thin
curses loop around them. Non-interactive: ``--list [--json]``, ``enable``,
``disable``.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

from curator import config as _config
from curator import paths
from curator.skill_utils import ESSENTIAL_SKILLS

# --- disabled-skills config ----------------------------------------------------------

def _normalize_skill_names(values) -> Set[str]:
    if values is None:
        return set()
    if isinstance(values, str):
        values = [values]
    try:
        return {str(v).strip() for v in values if str(v).strip()}
    except TypeError:
        return set()


def get_disabled_skills(cfg: Dict[str, Any], platform: Optional[str] = None) -> Set[str]:
    skills_cfg = cfg.get("skills") or {}
    if not isinstance(skills_cfg, dict):
        return set()
    disabled = _normalize_skill_names(skills_cfg.get("disabled"))
    if platform is not None:
        platform_disabled = _config.cfg_get(skills_cfg, "platform_disabled", platform)
        if platform_disabled is not None:
            disabled = disabled | _normalize_skill_names(platform_disabled)
    return disabled - ESSENTIAL_SKILLS


def save_disabled_skills(disabled: Set[str], platform: Optional[str] = None) -> None:
    """Essential skills are silently dropped — they cannot be disabled from any surface."""
    disabled = set(disabled) - ESSENTIAL_SKILLS

    def _mutate(cfg: Dict[str, Any]) -> None:
        skills = cfg.setdefault("skills", {})
        if platform is None:
            skills["disabled"] = sorted(disabled)
        else:
            skills.setdefault("platform_disabled", {})[platform] = sorted(disabled)
    _config.update_user_config(_mutate)


# --- editor ------------------------------------------------------------------------------

def default_editor_argv() -> List[str]:
    for var in ("VISUAL", "EDITOR"):
        value = os.environ.get(var, "").strip()
        if value:
            return shlex.split(value)
    return ["vi"]


def run_editor(path: Path) -> int:
    return subprocess.call([*default_editor_argv(), str(path)])


# --- model -------------------------------------------------------------------------------

class SkillsModel:
    """Everything the TUI shows or changes, with no terminal involved."""

    def disabled_names(self, platform: Optional[str] = None) -> Set[str]:
        return get_disabled_skills(_config.load_config_readonly(), platform)

    def rows(self, platform: Optional[str] = None) -> List[Dict[str, Any]]:
        from curator import skill_usage
        from curator.skills_tool import _find_all_skills
        meta = {s["name"]: s for s in _find_all_skills(skip_disabled=True)}
        disabled = self.disabled_names(platform)
        out: List[Dict[str, Any]] = []
        seen: Set[str] = set()
        for r in skill_usage.usage_report():
            name = r["name"]
            seen.add(name)
            m = meta.get(name, {})
            out.append(self._row(name, r, m, disabled, skill_usage._find_skill_dir(name)))
        for name in skill_usage.list_archived_skill_names():
            if name in seen:
                continue
            rec = {**skill_usage.get_record(name), "state": skill_usage.STATE_ARCHIVED}
            rec["owner"] = skill_usage.owner_of(rec, external=False)
            rec.update(last_activity_at=skill_usage.latest_activity_at(rec), activity_count=skill_usage.activity_count(rec))
            out.append(self._row(name, rec, {}, disabled, paths.archive_dir() / name))
        return sorted(out, key=lambda s: (s.get("category") or "", s["name"]))

    @staticmethod
    def _row(name, rec, meta, disabled, path) -> Dict[str, Any]:
        from curator import skill_usage
        return {
            "name": name, "category": meta.get("category"), "description": meta.get("description") or "",
            "owner": rec.get("owner", "user"), "state": rec.get("state", "active"),
            "pinned": bool(rec.get("pinned")), "managed": skill_usage._is_curator_managed_record(rec),
            "enabled": name not in disabled,
            "use_count": rec.get("use_count", 0), "view_count": rec.get("view_count", 0),
            "patch_count": rec.get("patch_count", 0), "activity_count": rec.get("activity_count", 0),
            "last_activity_at": rec.get("last_activity_at"), "path": path,
        }

    def categories(self, platform: Optional[str] = None) -> List[str]:
        return sorted({r["category"] or "uncategorized" for r in self.rows(platform)})

    def set_enabled(self, names, enabled: bool, platform: Optional[str] = None) -> None:
        disabled = self.disabled_names(platform)
        names = set(names)
        save_disabled_skills((disabled - names) if enabled else (disabled | names), platform)

    def toggle_enabled(self, name: str, platform: Optional[str] = None) -> bool:
        now_enabled = name in self.disabled_names(platform)
        self.set_enabled([name], now_enabled, platform)
        return now_enabled

    def set_category_enabled(self, category: str, enabled: bool, platform: Optional[str] = None) -> None:
        names = [r["name"] for r in self.rows(platform) if (r["category"] or "uncategorized") == category]
        self.set_enabled(names, enabled, platform)

    def category_enabled(self, category: str, platform: Optional[str] = None) -> bool:
        """A category is "enabled" when NOT all of its skills are disabled."""
        rows = [r for r in self.rows(platform) if (r["category"] or "uncategorized") == category]
        return not rows or not all(not r["enabled"] for r in rows)

    # lifecycle verbs — same rules as the CLI
    def adopt(self, name: str) -> Tuple[bool, str]:
        from curator import skill_usage
        return skill_usage.adopt_skill(name)

    def pin(self, name: str, pinned: bool) -> Tuple[bool, str]:
        from curator import skill_usage
        if not skill_usage.is_agent_created(name):
            return False, f"'{name}' lives only in skills.external_dirs — cannot pin"
        if not skill_usage.set_pinned(name, pinned):
            return False, f"could not {'pin' if pinned else 'unpin'} '{name}' — not curation-eligible"
        return True, f"{'pinned' if pinned else 'unpinned'} '{name}'"

    def archive(self, name: str) -> Tuple[bool, str]:
        from curator import skill_usage
        if skill_usage.get_record(name).get("pinned"):
            return False, f"'{name}' is pinned — unpin first"
        return self._as_user(skill_usage.archive_skill, name)

    def restore(self, name: str) -> Tuple[bool, str]:
        from curator import skill_usage
        return self._as_user(skill_usage.restore_skill, name)

    @staticmethod
    def _as_user(fn, name: str) -> Tuple[bool, str]:
        from curator import skill_ledger
        tok = skill_ledger.set_ledger_actor("user")
        try:
            return fn(name)
        finally:
            skill_ledger.reset_ledger_actor(tok)

    # browse / edit
    def _skill_dir(self, name: str) -> Optional[Path]:
        from curator import skill_usage
        found = skill_usage._find_skill_dir(name)
        if found is None:
            from curator.skill_manager import _find_skill
            hit = _find_skill(name)
            found = hit["path"] if hit else None
        if found is None:
            archived = paths.archive_dir() / name
            found = archived if (archived / "SKILL.md").exists() else None
        return found

    def view(self, name: str) -> str:
        skill_dir = self._skill_dir(name)
        if skill_dir is None:
            return f"skill '{name}' not found"
        try:
            text = (skill_dir / "SKILL.md").read_text(encoding="utf-8-sig", errors="replace")
        except OSError as e:
            return f"could not read {skill_dir / 'SKILL.md'}: {e}"
        support = sorted(str(p.relative_to(skill_dir)) for sub in ("references", "templates", "scripts", "assets")
                         if (skill_dir / sub).is_dir() for p in (skill_dir / sub).rglob("*") if p.is_file())
        footer = ("\n\n--- support files ---\n" + "\n".join(support)) if support else ""
        return f"{skill_dir}\n\n{text}{footer}"

    def edit(self, name: str, editor: Optional[Callable[[Path], int]] = None) -> Tuple[bool, str]:
        """Open SKILL.md in an editor; a changed file is ledgered (actor=user) and counted as a patch."""
        from curator import skill_ledger, skill_usage
        skill_dir = self._skill_dir(name)
        if skill_dir is None:
            return False, f"skill '{name}' not found"
        target = skill_dir / "SKILL.md"
        before_bytes = target.read_bytes() if target.exists() else b""
        before = skill_ledger.capture_before(skill_dir)
        rc = (editor or run_editor)(target)
        if rc not in (0, None):
            return False, f"editor exited {rc}"
        if not target.exists() or target.read_bytes() == before_bytes:
            return True, f"'{name}' unchanged"
        skill_ledger.record_mutation("edit", name, before=before if before is not None else [], after_root=skill_dir,
                                     actor="user", evidence={"editor": True})
        skill_usage.bump_patch(name, action="edit")
        return True, f"edited '{name}' (ledgered)"


# --- controller ------------------------------------------------------------------------------

_HELP = [
    "Skills — keys", "",
    "  ↑/k ↓/j  move        PgUp/PgDn  page       g/G  top/bottom",
    "  space    enable/disable this skill         c    enable/disable its whole category",
    "  Enter/v  view SKILL.md (pager)             e    edit SKILL.md in $EDITOR (ledgered)",
    "  p        pin/unpin   a  adopt   x  archive   r  restore",
    "  /        filter      Esc clear filter       ?   this help       q  quit", "",
    "Legend: [✓]/[ ] enabled · M managed · P pinned · state active/stale/archived · use/view/patch counts",
]


class Controller:
    def __init__(self, model: SkillsModel, *, editor: Optional[Callable[[Path], int]] = None,
                 platform: Optional[str] = None) -> None:
        self.model, self.editor, self.platform = model, editor, platform
        self.rows: List[Dict[str, Any]] = []
        self.cursor = 0
        self.filter_text = ""
        self.mode = "list"
        self.status = ""
        self.view_lines: List[str] = []
        self.view_offset = 0
        self.page = 10
        self.refresh()

    def refresh(self) -> None:
        self.rows = self.model.rows(self.platform)
        self.cursor = max(0, min(self.cursor, len(self.visible()) - 1))

    def visible(self) -> List[Dict[str, Any]]:
        q = self.filter_text.lower()
        if not q:
            return self.rows
        return [r for r in self.rows if q in r["name"].lower() or q in (r["description"] or "").lower()
                or q in (r["category"] or "").lower()]

    def current(self) -> Optional[Dict[str, Any]]:
        vis = self.visible()
        return vis[self.cursor] if 0 <= self.cursor < len(vis) else None

    def _act(self, result: Tuple[bool, str]) -> None:
        ok, msg = result
        self.status = msg
        self.refresh()

    def handle_key(self, key: str) -> bool:
        """Returns False when the UI should exit."""
        if self.mode == "filter":
            return self._filter_key(key)
        if self.mode == "view":
            return self._view_key(key)
        if self.mode == "help":
            self.mode = "list"
            return True
        return self._list_key(key)

    def _list_key(self, key: str) -> bool:
        vis = self.visible()
        cur = self.current()
        if key == "q":
            return False
        if key in ("KEY_DOWN", "j"):
            self.cursor = min(self.cursor + 1, max(0, len(vis) - 1))
        elif key in ("KEY_UP", "k"):
            self.cursor = max(0, self.cursor - 1)
        elif key == "KEY_NPAGE":
            self.cursor = min(self.cursor + self.page, max(0, len(vis) - 1))
        elif key == "KEY_PPAGE":
            self.cursor = max(0, self.cursor - self.page)
        elif key == "g":
            self.cursor = 0
        elif key == "G":
            self.cursor = max(0, len(vis) - 1)
        elif key == "/":
            self.mode = "filter"
        elif key == "KEY_ESC":
            self.filter_text = ""
            self.refresh()
        elif key == "?":
            self.mode = "help"
        elif cur is None:
            return True
        elif key == " ":
            if cur["name"] in ESSENTIAL_SKILLS:
                self.status = f"'{cur['name']}' is essential and cannot be disabled"
            else:
                enabled = self.model.toggle_enabled(cur["name"], self.platform)
                self.status = f"{'enabled' if enabled else 'disabled'} '{cur['name']}'"
                self.refresh()
        elif key == "c":
            category = cur["category"] or "uncategorized"
            enabled = self.model.category_enabled(category, self.platform)
            self.model.set_category_enabled(category, not enabled, self.platform)
            self.status = f"{'disabled' if enabled else 'enabled'} category '{category}'"
            self.refresh()
        elif key == "p":
            self._act(self.model.pin(cur["name"], not cur["pinned"]))
        elif key == "a":
            self._act(self.model.adopt(cur["name"]))
        elif key == "x":
            self._act(self.model.archive(cur["name"]))
        elif key == "r":
            self._act(self.model.restore(cur["name"]))
        elif key == "e":
            self._act(self.model.edit(cur["name"], editor=self.editor))
        elif key in ("\n", "v", "KEY_ENTER"):
            self.view_lines = self.model.view(cur["name"]).splitlines() or [""]
            self.view_offset = 0
            self.mode = "view"
        return True

    def _filter_key(self, key: str) -> bool:
        if key in ("\n", "KEY_ENTER"):
            self.mode = "list"
        elif key == "KEY_ESC":
            self.filter_text = ""
            self.mode = "list"
        elif key in ("KEY_BACKSPACE", "\x7f", "\b"):
            self.filter_text = self.filter_text[:-1]
        elif len(key) == 1 and key.isprintable():
            self.filter_text += key
        self.cursor = 0
        return True

    def _view_key(self, key: str) -> bool:
        last = max(0, len(self.view_lines) - 1)
        if key in ("q", "KEY_ESC"):
            self.mode = "list"
        elif key in ("KEY_DOWN", "j"):
            self.view_offset = min(self.view_offset + 1, last)
        elif key in ("KEY_UP", "k"):
            self.view_offset = max(0, self.view_offset - 1)
        elif key in ("KEY_NPAGE", " "):
            self.view_offset = min(self.view_offset + self.page, last)
        elif key == "KEY_PPAGE":
            self.view_offset = max(0, self.view_offset - self.page)
        elif key == "g":
            self.view_offset = 0
        elif key == "G":
            self.view_offset = last
        return True

    # --- rendering (plain strings; curses just paints them) ---

    def render_lines(self, width: int, height: int) -> List[str]:
        self.page = max(1, height - 4)
        if self.mode == "help":
            return [l[:width] for l in _HELP[:height]]
        if self.mode == "view":
            body = self.view_lines[self.view_offset:self.view_offset + max(1, height - 2)]
            head = f"view  (line {self.view_offset + 1}/{len(self.view_lines)})  q back"
            return [head[:width]] + [l[:width] for l in body]
        vis = self.visible()
        title = f"Skills ({len(vis)}{'/' + str(len(self.rows)) if self.filter_text else ''})"
        if self.filter_text or self.mode == "filter":
            title += f"  filter: {self.filter_text}{'▏' if self.mode == 'filter' else ''}"
        title += "   space toggle · Enter view · e edit · ? help · q quit"
        lines = [title[:width]]
        body_h = max(1, height - 3)
        top = min(max(0, self.cursor - body_h + 1), max(0, len(vis) - body_h))
        for i in range(top, min(len(vis), top + body_h)):
            lines.append(self.format_row(vis[i], width, is_cursor=(i == self.cursor)))
        lines.append("")
        lines.append((self.status or "")[:width])
        return lines[:height]

    @staticmethod
    def format_row(r: Dict[str, Any], width: int, *, is_cursor: bool) -> str:
        flags = ("M" if r["managed"] else "·") + ("P" if r["pinned"] else "·")
        counts = f"u{r['use_count']} v{r['view_count']} p{r['patch_count']}"
        cat = f"{r['category']}/" if r["category"] else ""
        line = (f" {'→' if is_cursor else ' '} [{'✓' if r['enabled'] else ' '}] {cat}{r['name']:<28.28} "
                f"{r['state']:<8} {flags} {counts:<14} {r['description']}")
        return line[:width]


# --- curses loop -------------------------------------------------------------------------------

def _decode_key(curses, code: int) -> str:
    names = {curses.KEY_UP: "KEY_UP", curses.KEY_DOWN: "KEY_DOWN", curses.KEY_NPAGE: "KEY_NPAGE",
             curses.KEY_PPAGE: "KEY_PPAGE", curses.KEY_ENTER: "\n", curses.KEY_BACKSPACE: "KEY_BACKSPACE",
             curses.KEY_RESIZE: "KEY_RESIZE"}
    if code in names:
        return names[code]
    if code == 27:
        return "KEY_ESC"
    if code in (10, 13):
        return "\n"
    if code == 127:
        return "KEY_BACKSPACE"
    try:
        return chr(code)
    except (ValueError, OverflowError):
        return ""


def run_tui(model: Optional[SkillsModel] = None, platform: Optional[str] = None) -> int:
    import curses

    model = model or SkillsModel()

    def _editor_in_curses(path: Path) -> int:
        curses.endwin()
        try:
            return run_editor(path)
        finally:
            curses.doupdate()

    def _loop(stdscr) -> int:
        curses.curs_set(0)
        stdscr.keypad(True)
        ctrl = Controller(model, editor=_editor_in_curses, platform=platform)
        while True:
            stdscr.erase()
            max_y, max_x = stdscr.getmaxyx()
            for y, line in enumerate(ctrl.render_lines(max_x - 1, max_y)):
                attr = curses.A_BOLD if y == 0 else (curses.A_REVERSE if line.startswith(" →") else curses.A_NORMAL)
                try:
                    stdscr.addnstr(y, 0, line, max_x - 1, attr)
                except curses.error:
                    pass
            stdscr.refresh()
            key = _decode_key(curses, stdscr.getch())
            if key == "KEY_RESIZE" or not key:
                continue
            if not ctrl.handle_key(key):
                return 0

    return curses.wrapper(_loop)


# --- CLI ---------------------------------------------------------------------------------------

def _print_table(rows: List[Dict[str, Any]]) -> None:
    print(f"  {'enabled':7s}  {'skill':36s}  {'state':8s}  {'flags':5s}  {'use':>4s}  {'view':>4s}  {'patch':>5s}  description")
    for r in rows:
        cat = f"{r['category']}/" if r["category"] else ""
        flags = ("M" if r["managed"] else "-") + ("P" if r["pinned"] else "-")
        print(f"  {'yes' if r['enabled'] else 'no':7s}  {(cat + r['name'])[:36]:36s}  {r['state']:8s}  {flags:5s}  "
              f"{r['use_count']:>4d}  {r['view_count']:>4d}  {r['patch_count']:>5d}  {r['description'][:50]}")


def cli_main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="curator skills",
                                     description="Enable/disable, browse and edit skills (TUI when no arguments).")
    parser.add_argument("verb", nargs="?", choices=("enable", "disable"), help="Non-interactive toggle")
    parser.add_argument("names", nargs="*", help="Skill names for enable/disable")
    parser.add_argument("--list", action="store_true", help="Print the skills table and exit")
    parser.add_argument("--json", action="store_true", help="With --list: JSON rows")
    parser.add_argument("--platform", default=None, help="Scope enable/disable to a platform (skills.platform_disabled)")
    args = parser.parse_args(argv)
    model = SkillsModel()
    if args.verb:
        if not args.names:
            print("curator: name at least one skill")
            return 1
        essential = [n for n in args.names if n in ESSENTIAL_SKILLS]
        if essential and args.verb == "disable":
            print(f"curator: {', '.join(essential)} is essential and cannot be disabled")
            return 1
        model.set_enabled(args.names, args.verb == "enable", args.platform)
        print(f"curator: {args.verb}d {', '.join(args.names)}")
        return 0
    if args.list:
        rows = model.rows(args.platform)
        if args.json:
            print(json.dumps([{**r, "path": str(r["path"]) if r["path"] else None} for r in rows], indent=2))
        elif rows:
            _print_table(rows)
        else:
            print("curator: no skills found")
        return 0
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        print("curator skills: interactive TUI needs a terminal; use --list, enable <names>, disable <names>.")
        return 1
    return run_tui(model, platform=args.platform)
