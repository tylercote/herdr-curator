"""Curator — background skill maintenance orchestrator (port of ``agent/curator.py``).

Inactivity-triggered (no cron daemon): when the agent is idle and the last run
is older than ``interval_hours``, ``maybe_run_curator()`` auto-transitions
lifecycle states from activity timestamps, optionally forks a review agent
that may pin/archive/consolidate/patch skills via ``skill_manage``, and
persists scheduler state in ``.curator_state``.

Invariants: only curator-managed skills are touched; never delete, only
archive (recoverable); pinned skills bypass all auto-transitions; the fork
uses the auxiliary slot and never touches the host agent's session.

A run has two phases:

1. **Automatic transitions** — pure, no LLM: unused > ``stale_after_days``
   -> ``stale``; unused > ``archive_after_days`` -> moved to ``.archive/``.
   Pinned and cron-referenced skills are skipped; never-used skills get a
   ``stale_after_days`` grace floor.
2. **LLM consolidation** — opt-in (``curator.consolidate``): the review prompt
   plus the candidate list go to a headless coding agent whose ONLY tools are
   ``skills_list`` / ``skill_view`` / ``skill_manage`` (see ``llm_review`` and
   ``mcp_server``). Its tool calls are audited to classify every removal as
   consolidated (absorbed into an umbrella) or pruned, cron references are
   rewritten, and ``run.json`` + ``REPORT.md`` land under ``logs/curator/``.
"""

from __future__ import annotations

import contextlib
import json
import logging
import re
import threading
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, NamedTuple, Optional, Set

from curator import paths, skill_usage
from curator.config import read_config_section
from curator.fsutil import atomic_json_write
from curator.prog import cmd as _cmd

logger = logging.getLogger(__name__)

DEFAULT_INTERVAL_HOURS, DEFAULT_MIN_IDLE_HOURS = 24 * 7, 2
DEFAULT_STALE_AFTER_DAYS, DEFAULT_ARCHIVE_AFTER_DAYS = 30, 90
DEFAULT_CONSOLIDATE = False


# --- .curator_state — persistent scheduler + status ---

def _state_file() -> Path:
    return paths.state_file()


def load_state() -> Dict[str, Any]:
    base: Dict[str, Any] = {
        "last_run_at": None, "last_run_duration_seconds": None, "last_run_summary": None,
        "last_run_summary_shown_at": None, "last_report_path": None, "paused": False, "run_count": 0,
    }
    path = _state_file()
    try:
        data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except (OSError, json.JSONDecodeError) as e:
        logger.debug("Failed to read curator state: %s", e)
        return base
    if isinstance(data, dict):
        base.update({k: v for k, v in data.items() if k in base or k.startswith("_")})
    return base


def save_state(data: Dict[str, Any]) -> None:
    try:
        atomic_json_write(_state_file(), data, indent=2, sort_keys=True)
    except Exception as e:
        logger.debug("Failed to save curator state: %s", e, exc_info=True)


def set_paused(paused: bool) -> None:
    save_state({**load_state(), "paused": bool(paused)})


def is_paused() -> bool:
    return bool(load_state().get("paused"))


# --- Config access ---

def _subdict(node: Any, *keys: str) -> Dict[str, Any]:
    for key in keys:
        node = node.get(key) if isinstance(node, dict) else None
    return node if isinstance(node, dict) else {}


def _load_config() -> Dict[str, Any]:
    return read_config_section("curator")


def _config_number(key: str, default, cast):
    try:
        return cast(_load_config().get(key, default))
    except (TypeError, ValueError):
        return default


def is_enabled() -> bool:
    return bool(_load_config().get("enabled", True))


def get_interval_hours() -> int:
    return _config_number("interval_hours", DEFAULT_INTERVAL_HOURS, int)


def get_min_idle_hours() -> float:
    return _config_number("min_idle_hours", DEFAULT_MIN_IDLE_HOURS, float)


def get_stale_after_days() -> int:
    return _config_number("stale_after_days", DEFAULT_STALE_AFTER_DAYS, int)


def get_archive_after_days() -> int:
    return _config_number("archive_after_days", DEFAULT_ARCHIVE_AFTER_DAYS, int)


def get_consolidate() -> bool:
    return bool(_load_config().get("consolidate", DEFAULT_CONSOLIDATE))


# --- Idle / interval check ---

def _parse_iso(ts: Optional[str]) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(ts) if ts else None
    except (TypeError, ValueError):
        return None


def should_run_now(now: Optional[datetime] = None) -> bool:
    """Gates: enabled, not paused, ``last_run_at`` present AND older than interval_hours. First
    observation seeds ``last_run_at`` to now and defers one interval. The idle check is the caller's."""
    if not is_enabled() or is_paused():
        return False
    state = load_state()
    last = _parse_iso(state.get("last_run_at"))
    now = now or datetime.now(timezone.utc)
    if last is None:
        try:
            state["last_run_at"] = now.isoformat()
            state["last_run_summary"] = (f"deferred first run — curator seeded, will run after one interval; "
                                         f"use `{_cmd('run --dry-run')}` to preview now")
            save_state(state)
        except Exception as e:  # pragma: no cover
            logger.debug("Failed to seed curator last_run_at: %s", e)
        return False
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    return (now - last) >= timedelta(hours=get_interval_hours())


# --- Automatic state transitions (pure function, no LLM) ---

def _cron_referenced_skills() -> Set[str]:
    try:
        from curator.cron_jobs import referenced_skill_names as _refs
        return _refs()
    except Exception as e:
        logger.debug("Curator could not read cron skill references: %s", e, exc_info=True)
        return set()


def _archive_as_curator(_u, name: str) -> bool:
    try:
        from curator.skill_ledger import reset_ledger_actor, set_ledger_actor
        tok = set_ledger_actor("curator")
    except Exception:
        tok = reset_ledger_actor = None  # type: ignore[assignment]
    try:
        return _u.archive_skill(name)[0]
    finally:
        if tok is not None:
            with contextlib.suppress(Exception):
                reset_ledger_actor(tok)


def apply_automatic_transitions(now: Optional[datetime] = None) -> Dict[str, int]:
    """Move every curator-managed skill between active/stale/archived based on its latest real
    activity; pinned and cron-referenced skills are never touched."""
    _u = skill_usage
    now = now or datetime.now(timezone.utc)
    stale_cutoff = now - timedelta(days=get_stale_after_days())
    archive_cutoff = now - timedelta(days=get_archive_after_days())
    protected = _cron_referenced_skills()
    counts = {"marked_stale": 0, "archived": 0, "reactivated": 0, "checked": 0}

    def _set(name: str, state: str, key: str) -> None:
        _u.set_state(name, state)
        counts[key] += 1

    for row in _u.curated_report():
        counts["checked"] += 1
        name = row["name"]
        if row.get("pinned") or name in protected:
            continue
        anchor = _parse_iso(row.get("last_activity_at")) or _parse_iso(row.get("created_at")) or now
        if anchor.tzinfo is None:
            anchor = anchor.replace(tzinfo=timezone.utc)
        current = row.get("state", _u.STATE_ACTIVE)
        if int(row.get("use_count", 0) or 0) == 0 and anchor > stale_cutoff:
            if current == _u.STATE_STALE:
                _set(name, _u.STATE_ACTIVE, "reactivated")
            continue
        if anchor <= archive_cutoff and current != _u.STATE_ARCHIVED:
            if _archive_as_curator(_u, name):
                counts["archived"] += 1
        elif anchor <= stale_cutoff and current == _u.STATE_ACTIVE:
            _set(name, _u.STATE_STALE, "marked_stale")
        elif anchor > stale_cutoff and current == _u.STATE_STALE:
            _set(name, _u.STATE_ACTIVE, "reactivated")
    return counts


# --- Review prompt for the forked agent ---

CURATOR_DRY_RUN_BANNER = (
    "═══════════════════════════════════════════════════════════════\n"
    "DRY-RUN — REPORT ONLY. DO NOT MUTATE THE SKILL LIBRARY.\n"
    "═══════════════════════════════════════════════════════════════\n"
    "\n"
    "This is a PREVIEW pass. Follow every instruction below EXCEPT:\n"
    "\n"
    "  • DO NOT call skill_manage with action=patch, create, delete, "
    "write_file, or remove_file.\n"
    "  • skills_list and skill_view are FINE — read as much as you need.\n"
    "\n"
    "Your output IS the deliverable. Produce the exact same "
    "human-readable summary and structured YAML block you would "
    "produce on a live run — but describe the actions you WOULD take, "
    "not actions you took. A downstream reviewer will read the report "
    "and decide whether to approve a live run with "
    f"`{_cmd('run')}` (no flag).\n"
    "\n"
    "If you accidentally take a mutating action, say so explicitly in "
    "the summary so the reviewer can revert it.\n"
    "═══════════════════════════════════════════════════════════════"
)


CURATOR_REVIEW_PROMPT = (
    "You are running as the background skill CURATOR. This is an "
    "UMBRELLA-BUILDING consolidation pass, not a passive audit and not a "
    "duplicate-finder.\n\n"
    "The goal of the skill collection is a LIBRARY OF CLASS-LEVEL "
    "INSTRUCTIONS AND EXPERIENTIAL KNOWLEDGE. A collection of hundreds of "
    "narrow skills where each one captures one session's specific bug is "
    "a FAILURE of the library — not a feature. An agent searching skills "
    "matches on descriptions, not on exact names (note: long descriptions "
    "are truncated to 57 chars in the system prompt skill index — keep the "
    "trigger class in that window). One broad umbrella "
    "skill with labeled subsections beats five narrow siblings for "
    "discoverability, not the other way around.\n\n"
    "The right target shape is CLASS-LEVEL skills whose SKILL.md carries the "
    "always-on rules and whose `references/`, `templates/`, and `scripts/` hold a "
    "SMALL set of topical depth — not one-session-one-skill micro-entries, and "
    "not an umbrella that hoards one references/ file per absorbed sibling. "
    "Consolidation means DISTILLING: the absorbed content becomes rules "
    "(imperative + one clause of why), the same lesson stated twice becomes "
    "one rule, and incident narration, PR/issue numbers, dates and quoted "
    "chatter are dropped — the rule must stand without the story. Moving a "
    "file unchanged under references/ is filing, not consolidating.\n\n"
    "Hard rules — do not violate:\n"
    "1. ONLY touch skills with owner=managed. skills_list shows an `owner` on "
    "every row: `managed` skills are the curator's own (it created them, or the "
    "user adopted them); `user` skills are hand-written and off-limits; "
    "`external` skills live in read-only directories. The candidate list below "
    "already contains ONLY managed skills. Never patch, archive, delete or "
    "absorb a user or external skill, and never create an umbrella whose "
    "content is copied from one — reading them for context is fine, that is all.\n"
    "2. DO NOT delete any skill. Archiving (moving the skill's directory "
    "into the skills tree's .archive/) is the maximum destructive action. "
    "Archives are recoverable; deletion is not.\n"
    "3. DO NOT touch skills shown as pinned=yes. Skip them entirely.\n"
    "3c. DO NOT archive or prune any skill marked `cron=yes` in the candidate "
    "list. A cron job depends on it and will fail to load it on its next "
    "run. You MAY still consolidate it into an umbrella — but only because "
    "the curator rewrites cron job skill references to follow consolidations; "
    "never simply prune it.\n"
    "5. DO NOT use usage counters as a reason to skip consolidation. The "
    "counters are new and often mostly zero. Judge overlap on CONTENT, "
    "not on use_count. 'use=0' is not evidence a skill is valuable; it's "
    "absence of evidence either way. Corollary: 'use=0' is ALSO not a "
    "reason to PRUNE a skill. Never archive a never-used skill (use=0) "
    "unless it is at least 30 days old (check last_activity / created date) "
    "AND its content is genuinely obsolete or fully absorbed elsewhere — a "
    "recently-created skill simply may not have had its trigger come up yet.\n"
    "6. DO NOT reject consolidation on the grounds that 'each skill has "
    "a distinct trigger'. Pairwise distinctness is the wrong bar. The "
    "right bar is: 'would a human maintainer write this as N separate "
    "skills, or as one skill with N labeled subsections?' When the "
    "answer is the latter, merge.\n\n"
    "How to work — not optional:\n"
    "1. Scan the full candidate list. Identify PREFIX CLUSTERS (skills "
    "sharing a first word or domain keyword). Examples you are likely "
    "to find: git-*, docker-*, testing-*, deploy-*, "
    "ollama-*, anthropic-*, gemini-*, mcp-*, salvage-*, pr-*, "
    "competitor-*, python-*, security-*, etc. Expect 10-25 clusters.\n"
    "2. For each cluster with 2+ members, do NOT ask 'are these pairs "
    "overlapping?' — ask 'what is the UMBRELLA CLASS these skills all "
    "serve? Would a maintainer name that class and write one skill for "
    "it?' If yes, pick (or create) the umbrella and absorb the siblings "
    "into it.\n"
    "3. Before building ANY umbrella, check skills_list for a user or "
    "external skill that already covers the class (skill_view it to be "
    "sure). The library the user hand-wrote or installed is the ground "
    "truth; the curator must never grow a parallel copy of it.\n"
    "4. Four ways to consolidate — use the right one per cluster:\n"
    "   d. ALREADY COVERED ELSEWHERE — a user or external skill already "
    "provides what a managed skill (or a would-be umbrella) does. Do NOT "
    "create or patch anything: archive the redundant managed skill with "
    "skill_manage action=delete absorbed_into=<that existing skill>. The "
    "existing skill is never modified; the archive is recorded as a "
    "consolidation into it. This is the PREFERRED move whenever it applies "
    "— prefer it over (a), (b) and (c).\n"
    "   a. MERGE INTO EXISTING UMBRELLA — one skill in the cluster is "
    "already broad enough to be the umbrella (example: `pr-triage-"
    "salvage` for the PR review cluster). Patch it to add a labeled "
    "section for each sibling's unique insight, then archive the "
    "siblings.\n"
    "   b. CREATE A NEW UMBRELLA SKILL.md — no existing member is broad "
    "enough. Use skill_manage action=create to write a new class-level "
    "skill whose SKILL.md covers the shared workflow and has short "
    "labeled subsections. Archive the now-absorbed narrow siblings.\n"
    "   c. DEMOTE TO REFERENCES/TEMPLATES/SCRIPTS — a sibling has "
    "narrow-but-valuable depth that is only needed sometimes. Distill it "
    "into the umbrella's appropriate support directory:\n"
    "      • `references/<topic>.md` — named by TOPIC, merged into an "
    "existing topical file when one covers it (decision tables, recipes, "
    "provider quirks, condensed domain notes). Never `<sibling-name>.md` "
    "copied verbatim; never a per-incident file.\n"
    "      • `templates/<name>.<ext>` for starter files meant to be "
    "copied and modified\n"
    "      • `scripts/<name>.<ext>` for statically re-runnable actions "
    "(verification scripts, fixture generators, probes)\n"
    "      Then archive the old sibling. Re-home the content through the "
    "LEDGERED tool surface: `skill_manage action=write_file` on the umbrella "
    "to place the file (subdirectories are created for you), then "
    "`skill_manage action=remove_file` on the source to drop the original, "
    "then `skill_manage action=delete` on the source. Never a terminal move "
    "— a shell mv/cp writes the same bytes with no ledger entry, so the "
    "archive that follows snapshots an already-stripped package and "
    f"`{_cmd('rollback')}` restores a hollow skill.\n\n"
    "Package integrity — not optional:\n"
    "Before demoting or archiving a skill, inspect it as a COMPLETE "
    "directory package, not just SKILL.md. A skill root may include "
    "`references/`, `templates/`, `scripts/`, and `assets/`; `skill_view` "
    "discovers those relative to the skill root. A reference markdown file "
    "inside another skill is NOT a new skill root and does not get its own "
    "linked-file discovery.\n"
    "If the source skill has support files OR SKILL.md contains relative "
    "links such as `references/...`, `templates/...`, `scripts/...`, or "
    "`assets/...`, DO NOT flatten only SKILL.md into "
    "`<umbrella>/references/<old>.md`. Choose one safe path instead:\n"
    "   • keep it as a standalone skill, OR\n"
    "   • fully merge it by re-homing every needed support file into the "
    "umbrella's canonical `references/`, `templates/`, `scripts/`, or "
    "`assets/` directories AND rewrite the destination instructions to "
    "the new paths, OR\n"
    "   • archive the entire original skill package unchanged.\n"
    "Never leave archived/demoted instructions pointing at files that were "
    "left behind under the old skill directory.\n"
    "5. Also flag skills whose NAME is too narrow (contains a PR number, "
    "a feature codename, a specific error string, an 'audit' / "
    "'diagnosis' / 'salvage' session artifact). These almost always "
    "belong as a subsection or support file under a class-level umbrella.\n"
    "6. Iterate. After one consolidation round, scan the remaining set "
    "and look for the NEXT umbrella opportunity. Don't stop after 3 "
    "merges.\n\n"
    "Your toolset:\n"
    "  - skills_list, skill_view        — read the current landscape\n"
    "    READ BEFORE WRITE — enforced, not advisory. Before skill_manage "
    "action=patch, action=edit, action=write_file on a file that already "
    "exists, or action=remove_file, call skill_view on that SAME target in "
    "this review turn — skill_view(name) for SKILL.md, "
    "skill_view(name, file_path=...) for a supporting file — and build the "
    "write from the content it just returned. A write without that read is "
    "REFUSED and nothing is saved.\n"
    "  - skill_manage action=patch      — add sections to the umbrella\n"
    "  - skill_manage action=create     — create a new umbrella SKILL.md\n"
    "  - skill_manage action=write_file — add a references/, templates/, "
    "or scripts/ file under an existing skill (the skill must already "
    "exist)\n"
    "  - skill_manage action=delete     — archive a skill. MUST pass "
    "`absorbed_into=<umbrella>` when you've merged its content into another "
    "skill, or `absorbed_into=\"\"` when you're truly pruning with no "
    "forwarding target. This drives cron-job skill-reference migration — "
    "guessing from your YAML summary after the fact is fragile.\n"
    "  You have NO terminal access in this pass — every filesystem mutation "
    "goes through skill_manage above so it is ledgered and rollback-able. "
    "Reading files works through skill_view (including "
    "skill_view(name, file_path=...) for support files).\n\n"
    "'keep' is a legitimate decision ONLY when the skill is already a "
    "class-level umbrella and none of the proposed merges would improve "
    "discoverability. 'This is narrow but distinct from its siblings' "
    "is NOT a reason to keep — it's a reason to move it under an "
    "umbrella as a subsection or support file.\n\n"
    "Expected output: real umbrella-ification. Process every obvious "
    "cluster. If you end the pass with fewer than 10 archives, you "
    "stopped too early — go back and look at the clusters you left "
    "alone.\n\n"
    "When done, write a human summary AND a structured machine-readable "
    "block so downstream tooling can distinguish consolidation from "
    "pruning. Format EXACTLY:\n\n"
    "## Structured summary (required)\n"
    "```yaml\n"
    "consolidations:\n"
    "  - from: <old-skill-name>\n"
    "    into: <umbrella-skill-name>\n"
    "    reason: <one short sentence — why merged, not just 'similar'>\n"
    "prunings:\n"
    "  - name: <skill-name>\n"
    "    reason: <one short sentence — why archived with no merge target>\n"
    "```\n\n"
    "Every skill you moved to .archive/ MUST appear in exactly one of the "
    "two lists. If you consolidated X into umbrella Y (patched Y, wrote "
    "a references file to Y, or created Y with X's content absorbed), X "
    "goes under `consolidations` with `into: Y`. If you archived X with "
    "no absorption — truly stale, irrelevant, or obsolete — X goes under "
    "`prunings`. Leave a list empty (`consolidations: []`) if none. Do "
    "not omit the block. The block comes AFTER your human-readable "
    "summary of clusters processed, patches made, and decisions left alone."
)


# --- Per-run reports — {YYYYMMDD-HHMMSS}/run.json + REPORT.md under logs/curator/ ---


def _reports_root() -> Path:
    root = paths.reports_root()
    try:
        root.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        logger.debug("Curator reports dir create failed: %s", e)
    return root


def _needle_in_path_component(needle: str, path: str) -> bool:
    norm_needle = needle.replace("-", "_")
    return any(part and part.rsplit(".", 1)[0].replace("-", "_") == norm_needle
               for part in path.replace("\\", "/").split("/"))


def _skill_manage_args(tc: Any, *, raw_fallback: bool) -> Optional[Dict[str, Any]]:
    if not isinstance(tc, dict) or tc.get("name") != "skill_manage":
        return None
    raw = tc.get("arguments") or ""
    if not isinstance(raw, str):
        return raw if isinstance(raw, dict) else None
    try:
        args = json.loads(raw)
    except Exception:
        return {"_raw": raw} if raw_fallback else None
    return args if isinstance(args, dict) else None


def _find_reference(args: Dict[str, Any], needles: Set[str]) -> Optional[str]:
    for key in ("file_path", "file_content", "content", "new_string", "_raw"):
        hay = args.get(key)
        if isinstance(hay, str) and any(
            (_needle_in_path_component(n, hay) if key == "file_path" else re.search(rf"\b{re.escape(n)}\b", hay))
            for n in filter(None, needles)
        ):
            return hay
    return None


def _visible_skill_names() -> Set[str]:
    """Every skill the pass could see — managed, user and external — so an ``absorbed_into`` that
    names an existing user/external skill classifies as a consolidation, not a prune."""
    try:
        from curator.skills_tool import _find_all_skills
        return {s["name"] for s in _find_all_skills()}
    except Exception:
        logger.debug("visible skill names unavailable", exc_info=True)
        return set()


def _classify_removed_skills(removed: List[str], added: List[str], after_names: Set[str],
                             tool_calls: List[Dict[str, Any]], visible: Optional[Set[str]] = None) -> Dict[str, List[Dict[str, Any]]]:
    """Split ``removed`` into consolidated vs pruned via tool-call evidence (earliest match wins)."""
    consolidated: List[Dict[str, Any]] = []
    pruned: List[Dict[str, Any]] = []
    parsed_calls = [a for a in (_skill_manage_args(tc, raw_fallback=True) for tc in tool_calls or []) if a is not None]
    destinations = set(after_names) | set(added or []) | set(visible or ())
    for name in filter(None, removed):
        needles = {name, name.replace("-", "_"), name.replace("_", "-")}
        for args in parsed_calls:
            target = args.get("name")
            if not isinstance(target, str) or not target or target == name or target not in destinations:
                continue
            hay = _find_reference(args, needles)
            if hay is not None:
                consolidated.append({"name": name, "into": target, "evidence":
                                     f"skill_manage action={args.get('action', '?')} on '{target}' referenced '{name}' in {hay[:80]}"})
                break
        else:
            pruned.append({"name": name})
    return {"consolidated": consolidated, "pruned": pruned}


def _parse_structured_summary(llm_final: str) -> Dict[str, List[Dict[str, str]]]:
    """Extract the required fenced ```yaml block. Tolerant: missing/malformed -> empty lists."""
    match = re.search(r"```ya?ml\s*\n(.*?)\n```", llm_final, re.DOTALL | re.IGNORECASE) if isinstance(llm_final, str) else None
    data = None
    if match:
        try:
            from curator.skill_utils import yaml_load
            data = yaml_load(match.group(1))
        except Exception:
            pass
    if not isinstance(data, dict):
        return {"consolidations": [], "prunings": []}

    def _entries(key: str, *fields: str) -> List[Dict[str, str]]:
        raw = data.get(key) or []
        entries = (e for e in raw if isinstance(e, dict)) if isinstance(raw, list) else ()
        cleaned = ({f: (v.strip() if isinstance((v := e.get(f)), str) else "") for f in (*fields, "reason")} for e in entries)
        return [e for e in cleaned if all(e[f] for f in fields)]

    return {"consolidations": _entries("consolidations", "from", "into"), "prunings": _entries("prunings", "name")}


def _extract_absorbed_into_declarations(tool_calls: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    out: Dict[str, Dict[str, Any]] = {}
    for args in (_skill_manage_args(tc, raw_fallback=False) for tc in tool_calls or []):
        if args is not None and args.get("action") == "delete":
            name, target = args.get("name"), args.get("absorbed_into")
            if isinstance(name, str) and name.strip() and isinstance(target, str):
                out[name.strip()] = {"into": target.strip(), "declared": True}
    return out


def _reconcile_classification(removed: List[str], heuristic: Dict[str, List[Dict[str, Any]]],
                              model_block: Dict[str, List[Dict[str, str]]], destinations: Set[str],
                              absorbed_declarations: Optional[Dict[str, Dict[str, Any]]] = None) -> Dict[str, List[Dict[str, Any]]]:
    """Merge heuristic (tool-call evidence) with the model's structured block. First match wins."""
    heur_cons = {e["name"]: e for e in heuristic.get("consolidated", [])}
    model_cons = {e["from"]: e for e in model_block.get("consolidations", [])}
    model_pruned = {e["name"]: e for e in model_block.get("prunings", [])}
    declared = absorbed_declarations or {}
    consolidated: List[Dict[str, Any]] = []
    pruned: List[Dict[str, Any]] = []
    for name in removed:
        mc, mp, hc, dec = model_cons.get(name), model_pruned.get(name), heur_cons.get(name), declared.get(name)
        mc_reason = (mc.get("reason") or "") if mc else ""
        hc_evidence = {"evidence": hc["evidence"]} if hc and hc.get("evidence") else {}

        def _cons(into: str, source: str, reason: str = "", **extra: Any) -> None:
            consolidated.append({"name": name, "into": into, "source": source, "reason": reason, **extra})

        def _prune(source: str, reason: str = "") -> None:
            pruned.append({"name": name, "source": source, "reason": reason})

        into_claim = dec.get("into", "") if dec is not None else None
        if into_claim and into_claim in destinations:
            _cons(into_claim, "absorbed_into (model-declared at delete)", mc_reason, **hc_evidence)
        elif into_claim == "":
            _prune("absorbed_into=\"\" (model-declared prune)", (mp.get("reason") or "") if mp else "")
        elif mc and mc.get("into") in destinations:
            _cons(mc["into"], "model" + ("+audit" if hc else ""), mc_reason, **hc_evidence)
        elif mc and hc:
            _cons(hc["into"], "tool-call audit (model named missing umbrella)", evidence=hc.get("evidence", ""),
                  model_claimed_into=mc["into"])
        elif mc:
            _prune("fallback (model named missing umbrella, no tool-call evidence)")
        elif hc:
            _cons(hc["into"], "tool-call audit (model omitted from structured block)", evidence=hc.get("evidence", ""))
        else:
            _prune("model" if mp else "no-evidence fallback", mp.get("reason", "") if mp else "")
    return {"consolidated": consolidated, "pruned": pruned}


class _RunDiff(NamedTuple):
    after_names: Set[str]
    removed: List[str]
    added: List[str]
    consolidated: List[Dict[str, Any]]
    pruned: List[Dict[str, Any]]


def _diff_and_classify(before_names: Set[str], after_names: Set[str], tool_calls: List[Dict[str, Any]], model_final: str,
                       visible: Optional[Set[str]] = None) -> _RunDiff:
    removed, added = sorted(before_names - after_names), sorted(after_names - before_names)
    visible = _visible_skill_names() if visible is None else visible
    destinations = set(after_names) | set(added) | visible
    classification = _reconcile_classification(
        removed=removed,
        heuristic=_classify_removed_skills(removed=removed, added=added, after_names=after_names, tool_calls=tool_calls, visible=visible),
        model_block=_parse_structured_summary(model_final), destinations=destinations,
        absorbed_declarations=_extract_absorbed_into_declarations(tool_calls),
    )
    return _RunDiff(after_names, removed, added, classification["consolidated"], classification["pruned"])


def _by_name(report: List[Dict[str, Any]]) -> Dict[Any, Dict[str, Any]]:
    return {r.get("name"): r for r in report if isinstance(r, dict)}


def _build_rename_summary(*, before_names: Set[str], after_report: List[Dict[str, Any]],
                          tool_calls: List[Dict[str, Any]], model_final: str) -> str:
    """The "where did my skills go?" lines appended to ``final_summary``; "" when nothing archived."""
    after_names = set(_by_name(after_report))
    if not before_names - after_names:
        return ""
    diff = _diff_and_classify(before_names, after_names, tool_calls, model_final)
    SHOW = 10
    total = len(diff.consolidated) + len(diff.pruned)
    entries = [f"  • {e.get('name', '?')} → {e.get('into', '?')}" for e in diff.consolidated]
    entries += [f"  • {e.get('name', '?') if isinstance(e, dict) else e} — pruned (stale)" for e in diff.pruned]
    lines = [f"archived {total} skill(s):"] + entries[:SHOW]
    if total > SHOW:
        lines.append(f"  … and {total - SHOW} more")
    lines.append(f"full report: {_cmd('status')}")
    umbrellas = sorted({e.get("into") for e in diff.consolidated if e.get("into")})
    if umbrellas:
        lines.append(f"keep an umbrella stable: {_cmd('pin ' + umbrellas[0])}")
    return "\n".join(lines)


def _rewrite_cron_refs(consolidated: List[Dict[str, Any]], pruned: List[Dict[str, Any]]) -> Dict[str, Any]:
    try:
        consolidated_map = {e["name"]: e["into"] for e in consolidated if isinstance(e, dict) and e.get("name") and e.get("into")}
        pruned_names = [e["name"] for e in pruned if isinstance(e, dict) and e.get("name")]
        if consolidated_map or pruned_names:
            from curator.cron_jobs import rewrite_skill_refs
            return rewrite_skill_refs(consolidated=consolidated_map, pruned=pruned_names)
        return {"rewrites": [], "jobs_updated": 0, "jobs_scanned": 0}
    except Exception as e:
        logger.debug("Curator cron skill rewrite failed: %s", e, exc_info=True)
        return {"rewrites": [], "jobs_updated": 0, "jobs_scanned": 0, "error": str(e)}


def _write_file(path: Path, label: str, render: Any) -> None:
    try:
        path.write_text(render() if callable(render) else json.dumps(render, indent=2, ensure_ascii=False) + "\n",
                        encoding="utf-8")
    except Exception as e:
        logger.debug("Curator %s write failed: %s", label, e)


def _write_run_report(*, started_at: datetime, elapsed_seconds: float, auto_counts: Dict[str, int], auto_summary: str,
                      before_report: List[Dict[str, Any]], before_names: Set[str], after_report: List[Dict[str, Any]],
                      llm_meta: Dict[str, Any]) -> Optional[Path]:
    """Write run.json + REPORT.md under logs/curator/{YYYYMMDD-HHMMSS}[-N]/."""
    root, stamp = _reports_root(), started_at.strftime("%Y%m%d-%H%M%S")
    run_dir, suffix = root / stamp, 1
    while run_dir.exists():
        suffix += 1
        run_dir = root / f"{stamp}-{suffix}"
    try:
        run_dir.mkdir(parents=True, exist_ok=False)
    except Exception as e:
        logger.debug("Curator run dir create failed: %s", e)
        return None
    tool_calls = llm_meta.get("tool_calls", []) or []
    after_by_name, before_by_name = _by_name(after_report), _by_name(before_report)
    diff = _diff_and_classify(before_names, set(after_by_name), tool_calls, llm_meta.get("final", "") or "")
    states = ((n, (before_by_name.get(n) or {}).get("state"), (after_by_name.get(n) or {}).get("state"))
              for n in sorted(diff.after_names & before_names))
    transitions = [{"name": n, "from": b, "to": a} for n, b, a in states if b and a and b != a]
    tc_counts: Dict[str, int] = dict(Counter(tc.get("name", "unknown") for tc in tool_calls))
    cron_rewrites = _rewrite_cron_refs(diff.consolidated, diff.pruned)
    jobs_updated = int(cron_rewrites.get("jobs_updated", 0))
    payload = {
        "started_at": started_at.isoformat(), "duration_seconds": round(elapsed_seconds, 2),
        "model": llm_meta.get("model", ""), "provider": llm_meta.get("provider", ""), "auto_transitions": auto_counts,
        "counts": {
            "before": len(before_names), "after": len(diff.after_names), "delta": len(diff.after_names) - len(before_names),
            "archived_this_run": len(diff.removed), "added_this_run": len(diff.added),
            "consolidated_this_run": len(diff.consolidated), "pruned_this_run": len(diff.pruned),
            "state_transitions": len(transitions), "cron_jobs_rewritten": jobs_updated,
            "tool_calls_total": sum(tc_counts.values()),
        },
        "tool_call_counts": tc_counts, "archived": diff.removed, "consolidated": diff.consolidated, "pruned": diff.pruned,
        "pruned_names": [p["name"] for p in diff.pruned], "added": diff.added, "state_transitions": transitions,
        "cron_rewrites": cron_rewrites,
        "llm_final": llm_meta.get("final", ""), "llm_summary": llm_meta.get("summary", ""),
        "llm_error": llm_meta.get("error"), "tool_calls": llm_meta.get("tool_calls", []),
    }
    _write_file(run_dir / "run.json", "run.json", payload)
    _write_file(run_dir / "REPORT.md", "REPORT.md", lambda: _render_report_markdown(payload))
    if jobs_updated > 0:
        _write_file(run_dir / "cron_rewrites.json", "cron_rewrites.json", cron_rewrites)
    return run_dir


def _reason_suffix(entry: Dict[str, Any]) -> str:
    return f" — {reason}" if (reason := (entry.get("reason") or "").strip()) else ""


def _consolidated_lines(entry: Dict[str, Any]) -> List[str]:
    line = f"- `{entry.get('name', '?')}` → merged into `{entry.get('into', '?')}`" + _reason_suffix(entry)
    source = entry.get("source", "")
    if source and source.startswith("tool-call audit"):
        line += f"  _(detected via {source})_"
    return [line] + ([f"  ⚠ The curator's summary named `{entry['model_claimed_into']}` "
                      "as the umbrella but that skill doesn't exist post-run; showing the tool-call audit's finding instead."]
                     if entry.get("model_claimed_into") else [])


def _pruned_lines(entry: Any) -> List[str]:
    return [f"- `{entry.get('name', '?')}`" + _reason_suffix(entry) if isinstance(entry, dict) else f"- `{entry}`"]


def _cron_rewrite_lines(entry: Dict[str, Any]) -> List[str]:
    job_name = entry.get("job_name") or entry.get("job_id") or "?"
    head = f"- `{job_name}`: `{', '.join(entry.get('before') or [])}` → `{', '.join(entry.get('after') or []) or '(none)'}`"
    return ([head] + [f"    - `{old}` → `{new}` (consolidated)" for old, new in (entry.get("mapped") or {}).items()]
            + [f"    - `{name}` dropped (pruned)" for name in (entry.get("dropped") or [])])


_REPORT_SECTIONS = (
    ("consolidated", "Consolidated into umbrella skills",
     "_These skills were **absorbed into another skill** during this run — their content still lives, just under a different name. "
     "The original directory was moved to the skills tree's `.archive/` for safety and can be restored via "
     f"`{_cmd('restore <name>')}` if the consolidation was wrong._\n", _consolidated_lines, 50, "see `run.json`"),
    ("pruned", "Pruned — archived for staleness",
     "_These skills were archived without being merged into an umbrella (e.g. stale, unused, or judged irrelevant). "
     f"Directories live under the skills tree's `.archive/`. Restore any via `{_cmd('restore <name>')}`._\n",
     _pruned_lines, 50, "see `run.json`"),
    ("added", "New skills this run", "_Usually these are new class-level umbrellas created via `skill_manage action=create`._\n",
     lambda n: [f"- `{n}`"], None, ""),
    ("state_transitions", "State transitions", None, lambda t: [f"- `{t.get('name')}`: {t.get('from')} → {t.get('to')}"], None, ""),
    ("cron_rewrites", "Cron job skill references rewritten",
     "_Cron jobs that referenced a consolidated or pruned skill were updated in-place so they keep loading the right instructions "
     "on their next run. See `cron_rewrites.json` for the full record._\n", _cron_rewrite_lines, 25, "see `cron_rewrites.json`"),
)


def _render_report_markdown(p: Dict[str, Any]) -> str:
    mins, secs = divmod(int(p.get("duration_seconds", 0) or 0), 60)
    dur_label = f"{mins}m {secs}s" if mins else f"{secs}s"
    counts, auto, tc_counts = p.get("counts") or {}, p.get("auto_transitions") or {}, p.get("tool_call_counts") or {}
    error = p.get("llm_error")
    lines = [
        f"# Curator run — {p.get('started_at', '')}\n",
        f"Model: `{p.get('model') or '(not resolved)'}` via `{p.get('provider') or '(not resolved)'}`  ·  Duration: {dur_label}  ·  "
        f"Agent-created skills: {counts.get('before', 0)} → {counts.get('after', 0)} ({counts.get('delta', 0):+d})\n",
        *([f"> ⚠ LLM pass error: `{error}`\n"] if error else []),
        "## Auto-transitions (pure, no LLM)\n", f"- checked: {auto.get('checked', 0)}", f"- marked stale: {auto.get('marked_stale', 0)}",
        f"- archived (no LLM, pure time-based staleness): {auto.get('archived', 0)}", f"- reactivated: {auto.get('reactivated', 0)}", "",
        "## LLM consolidation pass\n",
        f"- tool calls: **{counts.get('tool_calls_total', 0)}** (by name: {', '.join(f'{k}={v}' for k, v in sorted(tc_counts.items())) or 'none'})",
        f"- consolidated into umbrellas: **{counts.get('consolidated_this_run', 0)}**",
        f"- pruned (archived for staleness): **{counts.get('pruned_this_run', 0)}**", f"- new skills this run: **{counts.get('added_this_run', 0)}**",
        f"- state transitions (active ↔ stale ↔ archived): **{counts.get('state_transitions', 0)}**", "",
    ]
    for key, title, intro, render, show, hint in _REPORT_SECTIONS:
        items = p.get(key) or []
        if key == "cron_rewrites":
            items = items.get("rewrites") or []
        if not items:
            continue
        lines += [f"### {title} ({len(items)})\n"] + ([intro] if intro else [])
        for entry in items[:show]:
            lines += render(entry)
        lines += ([f"- … and {len(items) - show} more ({hint})"] if show is not None and len(items) > show else []) + [""]
    final = (p.get("llm_final") or "").strip()
    if final:
        lines += ["## LLM final summary\n", final, ""]
    elif not error and (p.get("llm_summary") or ""):
        lines += ["## LLM summary\n", p.get("llm_summary"), ""]
    lines += ["## Recovery\n", f"- Restore an archived skill: `{_cmd('restore <name>')}`",
              "- All archives live under the skills tree's `.archive/` and are recoverable by `mv`",
              "- See `run.json` in this directory for the full machine-readable record.", ""]
    return "\n".join(lines)


# --- Orchestrator — spawn the review fork for the LLM pass ---

def _render_candidate_list() -> str:
    rows = skill_usage.curated_report()
    if not rows:
        return "No curator-managed skills to review."
    cron_referenced = _cron_referenced_skills()
    return "\n".join([f"Curator-managed skills ({len(rows)}):\n"] + [
        f"- {r['name']}  state={r['state']}  "
        f"pinned={'yes' if r.get('pinned') else 'no'}  cron={'yes' if r['name'] in cron_referenced else 'no'}  "
        f"activity={r.get('activity_count', 0)}  use={r.get('use_count', 0)}  view={r.get('view_count', 0)}  "
        f"patches={r.get('patch_count', 0)}  last_activity={r.get('last_activity_at') or 'never'}"
        for r in rows
    ])


def _llm_meta(summary: str, error: Optional[str] = None) -> Dict[str, Any]:
    return {"final": "", "summary": summary, "model": "", "provider": "", "tool_calls": [], "error": error}


def _notify(on_summary: Optional[Callable[[str], None]], message: str) -> None:
    if on_summary:
        with contextlib.suppress(Exception):
            on_summary(message)


def _safe_curated_report() -> List[Dict[str, Any]]:
    with contextlib.suppress(Exception):
        return skill_usage.curated_report()
    return []


def _consolidation_pass(prefix: str, auto_summary: str, dry_run: bool, before_names: Set[str]) -> tuple:
    """The LLM half of a run: fork (unless no candidates), then append the rename map. Never raises."""
    try:
        candidate_list = _render_candidate_list()
        if candidate_list.startswith("No curator-managed skills"):
            final_summary = f"{prefix}{auto_summary}; llm: skipped (no candidates)"
            llm_meta = _llm_meta("skipped (no candidates)")
        else:
            prompt = f"{CURATOR_REVIEW_PROMPT}\n\n{candidate_list}"
            if dry_run:
                prompt = f"{CURATOR_DRY_RUN_BANNER}\n\n{prompt}"
            llm_meta = _run_llm_review(prompt)
            final_summary = f"{prefix}{auto_summary}; llm: {llm_meta.get('summary', 'no change')}"
    except Exception as e:
        logger.debug("Curator LLM pass failed: %s", e, exc_info=True)
        final_summary = f"{prefix}{auto_summary}; llm: error ({e})"
        llm_meta = _llm_meta(f"error ({e})", str(e))
    try:
        rename_lines = _build_rename_summary(
            before_names=before_names, after_report=skill_usage.curated_report(),
            tool_calls=llm_meta.get("tool_calls", []) or [], model_final=llm_meta.get("final", "") or "",
        )
        if rename_lines:
            final_summary = f"{final_summary}\n{rename_lines}"
    except Exception as e:
        logger.debug("Curator rename summary build failed: %s", e, exc_info=True)
    return final_summary, llm_meta


def run_curator_review(on_summary: Optional[Callable[[str], None]] = None, synchronous: bool = False,
                       dry_run: bool = False, consolidate: Optional[bool] = None) -> Dict[str, Any]:
    """One curator pass: (1) automatic transitions; (2) if *consolidate* and there are candidates,
    fork the review agent; (3) update .curator_state; (4) call *on_summary*. *dry_run* skips the
    transitions and instructs the fork to report only; REPORT.md is still written."""
    consolidate = get_consolidate() if consolidate is None else consolidate
    start = datetime.now(timezone.utc)
    if dry_run:
        counts = {"checked": len(_safe_curated_report()), "marked_stale": 0, "archived": 0, "reactivated": 0}
    else:
        try:
            from curator import curator_backup
            snap = curator_backup.snapshot_skills(reason="pre-curator-run")
            if snap is not None:
                _notify(on_summary, f"curator: snapshot created ({snap.name})")
        except Exception as e:
            logger.debug("Curator pre-run snapshot failed: %s", e, exc_info=True)
        counts = apply_automatic_transitions(now=start)
    auto_summary = ", ".join(
        f"{counts[key]} {label}" for key, label in (("marked_stale", "marked stale"), ("archived", "archived"), ("reactivated", "reactivated")) if counts[key]
    ) or "no changes"

    prefix = "dry-run auto: " if dry_run else "auto: "
    state = {**load_state(), "last_run_summary": f"{prefix}{auto_summary}"}
    if not dry_run:
        state.update(last_run_at=start.isoformat(), run_count=int(state.get("run_count", 0)) + 1)
    save_state(state)

    def _llm_pass():
        before_report = _safe_curated_report()
        before_names = set(_by_name(before_report))
        if consolidate:
            final_summary, llm_meta = _consolidation_pass(prefix, auto_summary, dry_run, before_names)
        else:
            final_summary = f"{prefix}{auto_summary}; llm: skipped (consolidation off)"
            llm_meta = _llm_meta("skipped (consolidation off)")
        elapsed = (datetime.now(timezone.utc) - start).total_seconds()
        state2 = {**load_state(), "last_run_duration_seconds": elapsed, "last_run_summary": final_summary}
        try:
            report_path = _write_run_report(
                started_at=start, elapsed_seconds=elapsed, auto_counts=counts, auto_summary=auto_summary,
                before_report=before_report, before_names=before_names, after_report=_safe_curated_report(), llm_meta=llm_meta,
            )
            if report_path is not None:
                state2["last_report_path"] = str(report_path)
        except Exception as e:
            logger.debug("Curator report write failed: %s", e, exc_info=True)
        save_state(state2)
        _notify(on_summary, f"curator: {final_summary}")

    if synchronous:
        _llm_pass()
    else:
        threading.Thread(target=_llm_pass, daemon=True, name="curator-review").start()
    return {"started_at": start.isoformat(), "auto_transitions": counts, "summary_so_far": auto_summary}


# --- Provider/model resolution for the review fork ---

class _ReviewRuntimeBinding(NamedTuple):
    """Provider/model for the curator review fork plus per-slot overrides."""
    provider: str
    model: str
    explicit_api_key: Optional[str]
    explicit_base_url: Optional[str]
    request_overrides: Dict[str, Any]


def _merge_request_overrides(runtime_overrides: Any, slot_extra_body: Any) -> Dict[str, Any]:
    merged = dict(runtime_overrides or {})
    if isinstance(slot_extra_body, dict) and slot_extra_body:
        merged["extra_body"] = {**(merged.get("extra_body") or {}), **slot_extra_body}
    return merged


def _resolve_review_runtime(cfg: Dict[str, Any]) -> _ReviewRuntimeBinding:
    """Precedence: ``auxiliary.curator.{provider,model}`` (both non-auto) > legacy
    ``curator.auxiliary.{provider,model}`` (deprecated) > main ``model.{provider,default/model}``."""
    def _slot(provider: str, model: str, slot: Dict[str, Any]) -> _ReviewRuntimeBinding:
        api_key, base_url = ((str(v).strip() or None) if v is not None else None for v in (slot.get("api_key"), slot.get("base_url")))
        return _ReviewRuntimeBinding(provider, model, api_key, base_url, _merge_request_overrides({}, slot.get("extra_body")))

    task = _subdict(cfg, "auxiliary", "curator")
    task_provider = (task.get("provider") or "").strip() or None
    task_model = (task.get("model") or "").strip() or None
    if task_provider and task_provider != "auto" and task_model:
        return _slot(task_provider, task_model, task)
    legacy = _subdict(cfg, "curator", "auxiliary")
    if legacy.get("provider") and legacy.get("model"):
        logger.info("curator: using deprecated curator.auxiliary.{provider,model} config — please migrate to auxiliary.curator.{provider,model}")
        return _slot(str(legacy["provider"]), str(legacy["model"]), legacy)
    main = _subdict(cfg, "model")
    return _ReviewRuntimeBinding(main.get("provider") or "auto", main.get("default") or main.get("model") or "", None, None, {})


def _run_llm_review(prompt: str) -> Dict[str, Any]:
    """Run the review fork (see ``llm_review``). Returns ``final``, ``summary`` (240-char cap),
    ``model``/``provider``, ``tool_calls`` ([{name, arguments}], truncated) and ``error``. Never raises."""
    try:
        from curator.llm_review import run_review
    except Exception as e:
        meta = _llm_meta("")
        meta["error"] = meta["summary"] = f"llm_review import failed: {e}"
        return meta
    return run_review(prompt)


# --- Public entrypoint for the session-start hook ---

def maybe_run_curator(*, idle_for_seconds: Optional[float] = None,
                      on_summary: Optional[Callable[[str], None]] = None) -> Optional[Dict[str, Any]]:
    """Best-effort: run a curator pass if all gates pass. Never raises."""
    try:
        if not should_run_now() or (idle_for_seconds is not None and idle_for_seconds < get_min_idle_hours() * 3600.0):
            return None
        return run_curator_review(on_summary=on_summary)
    except Exception as e:
        logger.debug("maybe_run_curator failed: %s", e, exc_info=True)
        return None
