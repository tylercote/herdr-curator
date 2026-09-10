"""curator.curator — consolidated-vs-pruned classifier, structured-summary parsing,
reconciliation and the rename summary."""

from __future__ import annotations

import json
from datetime import datetime, timezone

from conftest import write_skill


def _tc(**args):
    return {"name": "skill_manage", "arguments": json.dumps(args)}


def test_classify_consolidated_via_write_file_evidence(home):
    from curator import curator
    result = curator._classify_removed_skills(
        removed=["axolotl-training"], added=[], after_names={"training-platforms", "keeper"},
        tool_calls=[_tc(action="write_file", name="training-platforms", file_path="references/axolotl-training.md",
                        file_content="# Axolotl\n...")])
    assert result["consolidated"] == [{"name": "axolotl-training", "into": "training-platforms",
                                       "evidence": result["consolidated"][0]["evidence"]}]
    assert "write_file" in result["consolidated"][0]["evidence"]
    assert result["pruned"] == []


def test_classify_file_path_needs_whole_component_and_content_word_boundary(home):
    from curator import curator
    out = curator._classify_removed_skills(
        removed=["api", "test"], added=[], after_names={"umbrella"},
        tool_calls=[_tc(action="write_file", name="umbrella", file_path="references/api-design.md", file_content="latest"),
                    _tc(action="patch", name="umbrella", new_string="the latest news")])
    assert out["consolidated"] == [] and [p["name"] for p in out["pruned"]] == ["api", "test"]
    out = curator._classify_removed_skills(
        removed=["open-webui-setup"], added=[], after_names={"umbrella"},
        tool_calls=[_tc(action="write_file", name="umbrella", file_path="references/open_webui_setup.md", file_content="x")])
    assert out["consolidated"][0]["into"] == "umbrella"


def test_classify_ignores_calls_on_removed_or_vanished_targets_and_malformed_json(home):
    from curator import curator
    out = curator._classify_removed_skills(
        removed=["gone"], added=["new-umbrella"], after_names={"new-umbrella"},
        tool_calls=[_tc(action="patch", name="gone", new_string="gone"),
                    _tc(action="patch", name="vanished", new_string="gone"),
                    {"name": "skill_manage", "arguments": '{"action": "create", "name": "new-umbrella", "content": "absorbs gone'},
                    {"name": "skill_view", "arguments": json.dumps({"name": "gone"})}])
    # Parity: the malformed-JSON raw fallback carries no ``name``, so it can never be
    # consolidation evidence; calls on the removed skill itself or on a vanished
    # target are ignored too. Everything falls through to pruned.
    assert out["consolidated"] == [] and out["pruned"] == [{"name": "gone"}]
    parsed = curator._skill_manage_args({"name": "skill_manage", "arguments": "{bad"}, raw_fallback=True)
    assert parsed == {"_raw": "{bad"}
    assert curator._skill_manage_args({"name": "skill_manage", "arguments": "{bad"}, raw_fallback=False) is None
    assert curator._skill_manage_args({"name": "skill_manage", "arguments": {"action": "x"}}, raw_fallback=False) == {"action": "x"}


def test_report_md_splits_consolidated_and_pruned_sections(home):
    from curator import curator
    start = datetime.now(timezone.utc)
    before = [{"name": "absorbed-skill", "state": "active", "pinned": False},
              {"name": "dead-skill", "state": "stale", "pinned": False},
              {"name": "keeper", "state": "active", "pinned": False}]
    after = [{"name": "keeper", "state": "active", "pinned": False}, {"name": "umbrella", "state": "active", "pinned": False}]
    run_dir = curator._write_run_report(
        started_at=start, elapsed_seconds=60.0,
        auto_counts={"checked": 3, "marked_stale": 0, "archived": 0, "reactivated": 0}, auto_summary="no auto changes",
        before_report=before, before_names={r["name"] for r in before}, after_report=after,
        llm_meta={"final": "Consolidated absorbed-skill into umbrella. Pruned dead-skill.", "summary": "1 consolidated, 1 pruned",
                  "model": "m", "provider": "p", "error": None,
                  "tool_calls": [_tc(action="create", name="umbrella", content="# umbrella\n\nAbsorbed absorbed-skill.")]})
    payload = json.loads((run_dir / "run.json").read_text())
    assert {e["name"] for e in payload["consolidated"]} == {"absorbed-skill"}
    assert payload["pruned_names"] == ["dead-skill"]
    assert all(isinstance(e, dict) and "name" in e for e in payload["pruned"])
    assert set(payload["archived"]) == {"absorbed-skill", "dead-skill"}
    assert payload["counts"]["consolidated_this_run"] == 1 and payload["counts"]["pruned_this_run"] == 1
    md = (run_dir / "REPORT.md").read_text()
    assert "Consolidated into umbrella skills" in md and "Pruned — archived for staleness" in md
    assert "`absorbed-skill` → merged into `umbrella`" in md and "`dead-skill`" in md
    assert "### Skills archived" not in md


def test_parse_structured_summary_happy_path(home):
    from curator import curator
    text = ("Long human summary here.\n\n## Structured summary (required)\n```yaml\n"
            "consolidations:\n  - from: anthropic-api\n    into: llm-providers\n    reason: duplicate of the generic llm-providers skill\n"
            "  - from: openai-api\n    into: llm-providers\n    reason: same — merged with sibling\n"
            "prunings:\n  - name: random-old-notes\n    reason: pre-curator garbage, no overlap\n```\n")
    out = curator._parse_structured_summary(text)
    assert len(out["consolidations"]) == 2
    assert out["consolidations"][0] == {"from": "anthropic-api", "into": "llm-providers",
                                        "reason": "duplicate of the generic llm-providers skill"}
    assert out["prunings"] == [{"name": "random-old-notes", "reason": "pre-curator garbage, no overlap"}]


def test_parse_structured_summary_missing_or_malformed_block(home):
    from curator import curator
    assert curator._parse_structured_summary("No block in this text.") == {"consolidations": [], "prunings": []}
    assert curator._parse_structured_summary(None) == {"consolidations": [], "prunings": []}
    assert curator._parse_structured_summary("```yaml\n- just: a list\n```") == {"consolidations": [], "prunings": []}
    partial = curator._parse_structured_summary("```YAML\nconsolidations:\n  - from: a\n    into: b\n  - from: c\nprunings: []\n```")
    assert partial == {"consolidations": [{"from": "a", "into": "b", "reason": ""}], "prunings": []}
    # A ```json sample elsewhere must never be mistaken for the summary block.
    assert curator._parse_structured_summary("```json\n{\"consolidations\": []}\n```")["consolidations"] == []


def test_reconcile_model_block_visible_in_full_report(home):
    from curator import curator
    start = datetime.now(timezone.utc)
    before = [{"name": "anthropic-api", "state": "active", "pinned": False}, {"name": "stale-thing", "state": "stale", "pinned": False}]
    after = [{"name": "llm-providers", "state": "active", "pinned": False}]
    llm_final_text = ("Processed 3 clusters.\n\n## Structured summary (required)\n```yaml\n"
                      "consolidations:\n  - from: anthropic-api\n    into: llm-providers\n    reason: duplicate content, now a subsection\n"
                      "prunings:\n  - name: stale-thing\n    reason: pre-curator junk, no overlap with anything\n```\n")
    run_dir = curator._write_run_report(
        started_at=start, elapsed_seconds=30.0,
        auto_counts={"checked": 2, "marked_stale": 0, "archived": 0, "reactivated": 0}, auto_summary="none",
        before_report=before, before_names={r["name"] for r in before}, after_report=after,
        llm_meta={"final": llm_final_text, "summary": "1 consolidated, 1 pruned", "model": "m", "provider": "p", "error": None,
                  "tool_calls": [_tc(action="create", name="llm-providers", content="# llm-providers\nIncludes anthropic-api")]})
    payload = json.loads((run_dir / "run.json").read_text())
    cons = payload["consolidated"][0]
    assert cons["name"] == "anthropic-api" and cons["into"] == "llm-providers"
    assert cons["reason"] == "duplicate content, now a subsection" and cons["source"] == "model+audit"
    pruned = payload["pruned"][0]
    assert pruned["name"] == "stale-thing" and pruned["reason"] == "pre-curator junk, no overlap with anything"
    md = (run_dir / "REPORT.md").read_text()
    assert "duplicate content, now a subsection" in md and "pre-curator junk" in md


def test_extract_absorbed_into_picks_up_consolidation_and_ignores_non_delete(home):
    from curator import curator
    assert curator._extract_absorbed_into_declarations([_tc(action="delete", name="narrow-skill", absorbed_into="umbrella")]) == {
        "narrow-skill": {"into": "umbrella", "declared": True}}
    assert curator._extract_absorbed_into_declarations([_tc(action="patch", name="umbrella", absorbed_into="something")]) == {}
    assert curator._extract_absorbed_into_declarations([_tc(action="delete", name="x")]) == {}  # omitted -> absent
    assert curator._extract_absorbed_into_declarations([_tc(action="delete", name=" x ", absorbed_into="")]) == {
        "x": {"into": "", "declared": True}}


def test_reconcile_absorbed_into_beats_everything_else(home):
    from curator import curator
    out = curator._reconcile_classification(
        removed=["pr-review-format"], heuristic={"consolidated": [], "pruned": [{"name": "pr-review-format"}]},
        model_block={"consolidations": [], "prunings": []}, destinations={"agent-dev"},
        absorbed_declarations={"pr-review-format": {"into": "agent-dev", "declared": True}})
    assert out["pruned"] == [] and len(out["consolidated"]) == 1
    e = out["consolidated"][0]
    assert e["name"] == "pr-review-format" and e["into"] == "agent-dev" and "absorbed_into" in e["source"]


def test_reconcile_every_branch(home):
    from curator import curator
    out = curator._reconcile_classification(
        removed=["decl-missing", "model-into-missing-with-heur", "model-into-missing-no-heur", "heur-only", "model-prune", "nothing"],
        heuristic={"consolidated": [{"name": "model-into-missing-with-heur", "into": "real", "evidence": "ev1"},
                                    {"name": "heur-only", "into": "real", "evidence": "ev2"}],
                   "pruned": [{"name": "nothing"}]},
        model_block={"consolidations": [{"from": "model-into-missing-with-heur", "into": "ghost", "reason": "r1"},
                                        {"from": "model-into-missing-no-heur", "into": "ghost", "reason": "r2"}],
                     "prunings": [{"name": "model-prune", "reason": "stale"}]},
        destinations={"real"},
        absorbed_declarations={"decl-missing": {"into": "ghost", "declared": True}})
    cons = {e["name"]: e for e in out["consolidated"]}
    pruned = {e["name"]: e for e in out["pruned"]}
    assert cons["model-into-missing-with-heur"]["source"] == "tool-call audit (model named missing umbrella)"
    assert cons["model-into-missing-with-heur"]["model_claimed_into"] == "ghost"
    assert cons["heur-only"]["source"] == "tool-call audit (model omitted from structured block)"
    assert pruned["model-into-missing-no-heur"]["source"].startswith("fallback (model named missing umbrella")
    assert pruned["model-prune"] == {"name": "model-prune", "source": "model", "reason": "stale"}
    assert pruned["nothing"]["source"] == "no-evidence fallback"
    assert pruned["decl-missing"]["source"] == "no-evidence fallback"  # declared target missing -> fall through


def test_reconcile_mixed_declarations_and_legacy_calls(home):
    from curator import curator
    out = curator._reconcile_classification(
        removed=["declared-cons", "declared-prune", "legacy-cons", "legacy-prune"],
        heuristic={"consolidated": [{"name": "legacy-cons", "into": "umbrella-a", "evidence": "..."}],
                   "pruned": [{"name": "legacy-prune"}]},
        model_block={"consolidations": [], "prunings": []}, destinations={"umbrella-a", "umbrella-b"},
        absorbed_declarations={"declared-cons": {"into": "umbrella-b", "declared": True},
                               "declared-prune": {"into": "", "declared": True}})
    cons = {e["name"]: e for e in out["consolidated"]}
    pruned = {e["name"]: e for e in out["pruned"]}
    assert cons["declared-cons"]["into"] == "umbrella-b" and "absorbed_into" in cons["declared-cons"]["source"]
    assert cons["legacy-cons"]["into"] == "umbrella-a" and "tool-call audit" in cons["legacy-cons"]["source"]
    assert "model-declared prune" in pruned["declared-prune"]["source"]
    assert "no-evidence fallback" in pruned["legacy-prune"]["source"]


def test_rename_summary_empty_when_nothing_archived(home):
    from curator import curator
    assert curator._build_rename_summary(
        before_names={"alpha", "beta"},
        after_report=[{"name": "alpha", "state": "active"}, {"name": "beta", "state": "active"}],
        tool_calls=[], model_final="") == ""


def test_rename_summary_pruned_marked_explicitly(home):
    from curator import curator
    result = curator._build_rename_summary(
        before_names={"old-flaky-thing", "keeper"}, after_report=[{"name": "keeper", "state": "active"}],
        tool_calls=[_tc(action="delete", name="old-flaky-thing", absorbed_into="")], model_final="")
    assert "old-flaky-thing — pruned (stale)" in result
    assert "keep an umbrella stable" not in result
    assert result.splitlines()[-1] == "full report: curator status"


def test_rename_summary_caps_at_ten_with_more_indicator(home):
    from curator import curator
    removed = [f"skill-{i}" for i in range(15)]
    tool_calls = [_tc(action="delete", name=name, absorbed_into="umbrella") for name in removed]
    result = curator._build_rename_summary(
        before_names=set(removed) | {"umbrella"}, after_report=[{"name": "umbrella", "state": "active"}],
        tool_calls=tool_calls, model_final="")
    assert "archived 15 skill(s):" in result and "… and 5 more" in result
    assert sum(1 for ln in result.splitlines() if ln.startswith("  • ")) == 10
    assert "keep an umbrella stable: curator pin umbrella" in result


def test_rename_summary_mixed_consolidation_and_pruning(home):
    from curator import curator
    result = curator._build_rename_summary(
        before_names={"merge-me", "drop-me", "umbrella"}, after_report=[{"name": "umbrella", "state": "active"}],
        tool_calls=[_tc(action="delete", name="merge-me", absorbed_into="umbrella"),
                    _tc(action="delete", name="drop-me", absorbed_into="")], model_final="")
    lines = result.splitlines()
    merge_idx = next(i for i, ln in enumerate(lines) if "merge-me" in ln)
    drop_idx = next(i for i, ln in enumerate(lines) if "drop-me" in ln)
    assert merge_idx < drop_idx
    assert "merge-me → umbrella" in lines[merge_idx] and "drop-me — pruned (stale)" in lines[drop_idx]


def _delete_call(name, absorbed_into):
    return {"name": "skill_manage", "arguments": json.dumps({"action": "delete", "name": name, "absorbed_into": absorbed_into})}


def test_absorbed_into_user_or_external_skill_is_a_consolidation_not_a_prune(home):
    from curator import curator
    # explicit visible set: the target is a user/external skill, so it is in neither before nor after (managed) sets
    diff = curator._diff_and_classify({"dup"}, set(), [_delete_call("dup", "polish")], "", visible={"polish"})
    assert [(e["name"], e["into"]) for e in diff.consolidated] == [("dup", "polish")] and diff.pruned == []
    # without visibility the same declaration would have been misfiled as a prune
    diff = curator._diff_and_classify({"dup"}, set(), [_delete_call("dup", "polish")], "", visible=set())
    assert diff.consolidated == [] and [e["name"] for e in diff.pruned] == ["dup"]


def test_rename_summary_uses_real_visible_skills(home, tmp_path, set_config):
    from curator import curator, skill_usage
    write_skill(tmp_path / "ext", "polish")
    set_config({"skills": {"external_dirs": [str(tmp_path / "ext")]}})
    write_skill(home / "skills", "dup")
    skill_usage.mark_agent_created("dup")
    before = {"dup"}
    assert skill_usage.archive_skill("dup")[0]
    summary = curator._build_rename_summary(before_names=before, after_report=skill_usage.curated_report(),
                                            tool_calls=[_delete_call("dup", "polish")], model_final="")
    assert "dup → polish" in summary and "pruned" not in summary
