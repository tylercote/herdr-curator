"""curator.curator — orchestrator, idle gating, state transitions.

LLM spawning is never exercised here — ``_run_llm_review`` is monkeypatched so
tests run fully offline.
"""

from __future__ import annotations

import json
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from conftest import write_skill


@pytest.fixture
def cur(home, monkeypatch):
    from curator import curator as c
    monkeypatch.setattr(c, "_run_llm_review", lambda prompt: {
        "final": "", "summary": "llm-stub", "model": "", "provider": "", "tool_calls": [], "error": None})
    yield c
    for t in threading.enumerate():
        if t.name == "curator-review" and t.is_alive():
            t.join(timeout=10.0)


def _llm_meta(**over):
    base = {"final": "", "summary": "s", "model": "", "provider": "", "tool_calls": [], "error": None}
    base.update(over)
    return base


# ---------------------------------------------------------------------------
# Config gates
# ---------------------------------------------------------------------------

def test_curator_defaults(cur):
    assert cur.get_interval_hours() == 24 * 7
    assert cur.get_min_idle_hours() == 2
    assert cur.get_stale_after_days() == 30
    assert cur.get_archive_after_days() == 90
    assert cur.is_enabled() is True
    assert cur.get_consolidate() is False


def test_config_overrides_and_bad_values(cur, set_config):
    set_config({"curator": {"interval_hours": "12", "min_idle_hours": "bogus", "enabled": False,
                            "consolidate": True}})
    assert cur.get_interval_hours() == 12
    assert cur.get_min_idle_hours() == 2  # cast failure -> default
    assert cur.is_enabled() is False
    assert cur.get_consolidate() is True


# ---------------------------------------------------------------------------
# should_run_now
# ---------------------------------------------------------------------------

def test_first_run_defers(cur):
    assert cur.should_run_now() is False
    state = cur.load_state()
    assert state.get("last_run_at") is not None
    assert "deferred first run" in state["last_run_summary"]
    assert cur.should_run_now() is False


def test_runs_after_interval_and_respects_pause_and_disable(cur, set_config):
    old = (datetime.now(timezone.utc) - timedelta(hours=24 * 8)).isoformat()
    cur.save_state({**cur.load_state(), "last_run_at": old})
    assert cur.should_run_now() is True
    cur.set_paused(True)
    assert cur.should_run_now() is False
    cur.set_paused(False)
    set_config({"curator": {"enabled": False}})
    assert cur.should_run_now() is False


def test_should_run_now_treats_naive_timestamps_as_utc(cur):
    naive = (datetime.now(timezone.utc) - timedelta(hours=24 * 8)).replace(tzinfo=None).isoformat()
    cur.save_state({**cur.load_state(), "last_run_at": naive})
    assert cur.should_run_now() is True


def test_set_paused_roundtrip(cur):
    cur.set_paused(True)
    assert cur.is_paused() is True
    cur.set_paused(False)
    assert cur.is_paused() is False


def test_load_state_ignores_unknown_keys_but_keeps_private(cur):
    cur.save_state({"paused": True, "junk": 1, "_ext": 2})
    state = cur.load_state()
    assert state["paused"] is True and "junk" not in state and state["_ext"] == 2
    cur._state_file().write_text("{ nope", encoding="utf-8")
    assert cur.load_state()["paused"] is False


# ---------------------------------------------------------------------------
# Automatic state transitions
# ---------------------------------------------------------------------------

def _backdate(u, name: str, days: int, *, use_count: int = 1, state: str = "active"):
    ts = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    data = u.load_usage()
    data[name] = u._empty_record()
    data[name]["created_by"] = "agent"
    data[name]["created_at"] = ts
    data[name]["last_used_at"] = ts if use_count else None
    data[name]["use_count"] = use_count
    data[name]["state"] = state
    u.save_usage(data)


def test_transitions_stale_archive_reactivate(cur, home):
    from curator import skill_usage as u
    skills = home / "skills"
    for name in ("fresh", "old", "ancient", "revived"):
        write_skill(skills, name)
    _backdate(u, "fresh", 1)
    _backdate(u, "old", 45)
    _backdate(u, "ancient", 200)
    _backdate(u, "revived", 1, state="stale")
    counts = cur.apply_automatic_transitions()
    assert counts == {"marked_stale": 1, "archived": 1, "reactivated": 1, "checked": 4}
    usage = u.load_usage()
    assert usage["fresh"]["state"] == "active"
    assert usage["old"]["state"] == "stale"
    assert usage["ancient"]["state"] == "archived"
    assert (skills / ".archive" / "ancient").is_dir() and not (skills / "ancient").exists()
    assert usage["revived"]["state"] == "active"


def test_archive_transition_is_ledgered_as_curator(cur, home):
    from curator import skill_ledger, skill_usage as u
    write_skill(home / "skills", "ancient")
    _backdate(u, "ancient", 200)
    cur.apply_automatic_transitions()
    rows = [r for r in skill_ledger.list_entries(skill="ancient") if r["action"] == "archive"]
    assert len(rows) == 1 and rows[0]["actor"] == "curator"


def test_pinned_skill_is_never_touched(cur, home):
    from curator import skill_usage as u
    write_skill(home / "skills", "precious")
    super_old = (datetime.now(timezone.utc) - timedelta(days=365)).isoformat()
    data = u.load_usage()
    data["precious"] = u._empty_record()
    data["precious"].update(created_by="agent", last_used_at=super_old, created_at=super_old, pinned=True)
    u.save_usage(data)
    counts = cur.apply_automatic_transitions()
    assert counts["archived"] == 0 and counts["marked_stale"] == 0
    rec = u.get_record("precious")
    assert rec["state"] == "active" and rec["pinned"] is True


def test_never_used_skill_gets_grace_floor(cur, home):
    from curator import skill_usage as u
    skills = home / "skills"
    write_skill(skills, "young-unused")
    write_skill(skills, "old-unused")
    write_skill(skills, "young-unused-stale")
    _backdate(u, "young-unused", 10, use_count=0)
    _backdate(u, "old-unused", 200, use_count=0)
    _backdate(u, "young-unused-stale", 10, use_count=0, state="stale")
    counts = cur.apply_automatic_transitions()
    usage = u.load_usage()
    assert usage["young-unused"]["state"] == "active"
    assert usage["old-unused"]["state"] == "archived"  # anchored on created_at, past the floor
    assert usage["young-unused-stale"]["state"] == "active"
    assert counts["reactivated"] == 1


def test_candidate_list_marks_cron_referenced_skills(cur, home, monkeypatch):
    from curator import skill_usage as u
    skills = home / "skills"
    write_skill(skills, "cron-dep")
    write_skill(skills, "plain")
    _backdate(u, "cron-dep", 1)
    _backdate(u, "plain", 1)
    monkeypatch.setattr(cur, "_cron_referenced_skills", lambda: {"cron-dep"})
    listing = cur._render_candidate_list()
    cron_line = next(l for l in listing.splitlines() if l.startswith("- cron-dep"))
    plain_line = next(l for l in listing.splitlines() if l.startswith("- plain"))
    assert "cron=yes" in cron_line and "cron=no" in plain_line
    assert "pinned=no" in plain_line


def test_candidate_list_empty(cur):
    assert cur._render_candidate_list() == "No curator-managed skills to review."


def _write_cron_job(home: Path, skill_ref: str):
    cron_dir = home / "cron"
    cron_dir.mkdir(parents=True, exist_ok=True)
    (cron_dir / "jobs.json").write_text(json.dumps([{
        "id": "job1", "name": "quarterly digest", "enabled": True, "prompt": "write the digest",
        "skills": [skill_ref], "schedule": {"kind": "cron", "expr": "0 9 1 */3 *"}}]), encoding="utf-8")


def test_cron_referenced_skill_by_name_survives_inactivity(cur, home):
    from curator import skill_usage as u
    write_skill(home / "skills", "quarterly-report")
    _backdate(u, "quarterly-report", 200)
    _write_cron_job(home, "quarterly-report")
    counts = cur.apply_automatic_transitions()
    assert counts["archived"] == 0
    assert u.load_usage()["quarterly-report"]["state"] == u.STATE_ACTIVE


def test_cron_referenced_skill_by_absolute_path_survives_inactivity(cur, home):
    from curator import skill_usage as u
    skills = home / "skills"
    write_skill(skills, "quarterly-report")
    _backdate(u, "quarterly-report", 200)
    _write_cron_job(home, str(skills / "quarterly-report"))
    counts = cur.apply_automatic_transitions()
    assert counts["archived"] == 0
    assert u.load_usage()["quarterly-report"]["state"] == u.STATE_ACTIVE


def test_unreferenced_skill_is_still_archived(cur, home):
    from curator import skill_usage as u
    skills = home / "skills"
    write_skill(skills, "quarterly-report")
    write_skill(skills, "orphan")
    _backdate(u, "quarterly-report", 200)
    _backdate(u, "orphan", 200)
    _write_cron_job(home, str(skills / "quarterly-report"))
    cur.apply_automatic_transitions()
    usage = u.load_usage()
    assert usage["quarterly-report"]["state"] == u.STATE_ACTIVE
    assert usage["orphan"]["state"] == u.STATE_ARCHIVED


def test_corrupt_cron_store_never_crashes_transitions(cur, home):
    (home / "cron").mkdir()
    (home / "cron" / "jobs.json").write_text("{ nope", encoding="utf-8")
    assert cur._cron_referenced_skills() == set()
    assert cur.apply_automatic_transitions()["checked"] == 0



def test_run_review_records_state(cur, home):
    from curator import skill_usage as u
    write_skill(home / "skills", "a")
    u.mark_agent_created("a")
    result = cur.run_curator_review(synchronous=True)
    assert "started_at" in result and "auto_transitions" in result
    state = cur.load_state()
    assert state["last_run_at"] is not None
    assert state["run_count"] >= 1
    assert state["last_run_summary"] == "auto: no changes; llm: skipped (consolidation off)"
    assert state["last_run_duration_seconds"] is not None
    assert state["last_report_path"] and Path(state["last_report_path"]).is_dir()


def test_prune_only_run_never_forks(cur, home, monkeypatch):
    from curator import skill_usage as u
    write_skill(home / "skills", "a")
    u.mark_agent_created("a")
    calls = []
    monkeypatch.setattr(cur, "_run_llm_review", lambda prompt: calls.append(prompt) or _llm_meta())
    cur.run_curator_review(synchronous=True)
    assert calls == []


def test_consolidate_skips_llm_when_no_candidates(cur, home, monkeypatch):
    calls = []
    monkeypatch.setattr(cur, "_run_llm_review", lambda prompt: calls.append(prompt) or _llm_meta())
    cur.run_curator_review(synchronous=True, consolidate=True)
    assert calls == []
    assert cur.load_state()["last_run_summary"] == "auto: no changes; llm: skipped (no candidates)"


def test_dry_run_injects_report_only_banner(cur, home, monkeypatch):
    from curator import skill_usage as u
    write_skill(home / "skills", "a")
    u.mark_agent_created("a")
    captured = {}
    monkeypatch.setattr(cur, "_run_llm_review", lambda prompt: captured.setdefault("prompt", prompt) and _llm_meta())
    cur.run_curator_review(synchronous=True, dry_run=True, consolidate=True)
    assert "DRY-RUN" in captured["prompt"] and "DO NOT" in captured["prompt"]


def test_dry_run_does_not_bump_run_clock_or_mutate(cur, home):
    from curator import skill_usage as u
    write_skill(home / "skills", "ancient")
    _backdate(u, "ancient", 200)
    cur.run_curator_review(synchronous=True, dry_run=True)
    state = cur.load_state()
    assert state["last_run_at"] is None and state["run_count"] == 0
    assert state["last_run_summary"].startswith("dry-run auto: ")
    assert u.load_usage()["ancient"]["state"] == "active"
    assert state["last_report_path"]


def test_run_review_synchronous_invokes_llm_stub(cur, home, monkeypatch):
    from curator import skill_usage as u
    write_skill(home / "skills", "a")
    u.mark_agent_created("a")
    calls = []
    monkeypatch.setattr(cur, "_run_llm_review", lambda prompt: calls.append(prompt) or _llm_meta(
        final="stubbed-summary", summary="stubbed-summary", model="stub-model", provider="stub-provider"))
    captured = []
    cur.run_curator_review(on_summary=captured.append, synchronous=True, consolidate=True)
    assert len(calls) == 1 and "CURATOR" in calls[0]
    assert any("stubbed-summary" in s for s in captured)
    payload = json.loads((Path(cur.load_state()["last_report_path"]) / "run.json").read_text())
    assert payload["model"] == "stub-model" and payload["provider"] == "stub-provider"


def test_run_review_background_thread_completes(cur, home, monkeypatch):
    from curator import skill_usage as u
    write_skill(home / "skills", "a")
    u.mark_agent_created("a")
    done = threading.Event()
    monkeypatch.setattr(cur, "_run_llm_review", lambda prompt: _llm_meta(final="bg", summary="bg"))
    cur.run_curator_review(on_summary=lambda m: done.set() if "bg" in m else None, synchronous=False, consolidate=True)
    assert done.wait(10)


def test_llm_error_is_recorded_not_raised(cur, home, monkeypatch):
    from curator import skill_usage as u
    write_skill(home / "skills", "a")
    u.mark_agent_created("a")

    def _boom(prompt):
        raise RuntimeError("kaboom")
    monkeypatch.setattr(cur, "_run_llm_review", _boom)
    cur.run_curator_review(synchronous=True, consolidate=True)
    assert "llm: error (kaboom)" in cur.load_state()["last_run_summary"]


def test_maybe_run_curator_gates(cur, home, monkeypatch):
    calls = []
    monkeypatch.setattr(cur, "run_curator_review", lambda **kw: calls.append(kw) or {"started_at": "x"})
    assert cur.maybe_run_curator() is None  # first observation seeds + defers
    old = (datetime.now(timezone.utc) - timedelta(hours=24 * 8)).isoformat()
    cur.save_state({**cur.load_state(), "last_run_at": old})
    assert cur.maybe_run_curator(idle_for_seconds=60) is None  # not idle long enough
    assert cur.maybe_run_curator(idle_for_seconds=3 * 3600) == {"started_at": "x"}
    assert cur.maybe_run_curator() == {"started_at": "x"}  # no measurement = not enforced
    assert len(calls) == 2


def test_maybe_run_curator_never_raises(cur, monkeypatch):
    def _boom():
        raise RuntimeError("x")
    monkeypatch.setattr(cur, "should_run_now", _boom)
    assert cur.maybe_run_curator() is None


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

def test_state_atomic_write_no_tmp_leftovers(cur):
    cur.save_state({"paused": True})
    parent = cur._state_file().parent
    assert [p.name for p in parent.iterdir() if p.name.endswith(".tmp")] == []


# ---------------------------------------------------------------------------
# Prompt invariants
# ---------------------------------------------------------------------------

def test_curator_does_not_instruct_model_to_pin(cur):
    lines = cur.CURATOR_REVIEW_PROMPT.split("\n")
    assert not any(l.strip().startswith("pin ") for l in lines)


def test_review_prompt_tells_reviewer_to_read_before_writing(cur, home, monkeypatch):
    from curator import skill_usage as u
    write_skill(home / "skills", "a")
    u.mark_agent_created("a")
    captured = {}
    monkeypatch.setattr(cur, "_run_llm_review", lambda prompt: captured.setdefault("prompt", prompt) and _llm_meta())
    cur.run_curator_review(synchronous=True, consolidate=True)
    prompt = captured["prompt"]
    assert "skill_view" in prompt
    for action in ("edit", "patch", "write_file", "remove_file"):
        assert f"action={action}" in prompt


def test_review_prompt_does_not_steer_terminal_writes(cur):
    for text in (cur.CURATOR_REVIEW_PROMPT, cur.CURATOR_DRY_RUN_BANNER):
        assert "mkdir -p" not in text and "&& mv" not in text


def test_review_runtime_passes_auxiliary_curator_credentials(cur):
    cfg = {"model": {"provider": "openrouter", "default": "openai/gpt-5.5"},
           "auxiliary": {"curator": {"provider": "custom", "model": "local-mini", "api_key": "sk-curator-only",
                                     "base_url": "http://localhost:11434/v1"}}}
    binding = cur._resolve_review_runtime(cfg)
    assert binding.provider == "custom" and binding.model == "local-mini"
    assert binding.explicit_api_key == "sk-curator-only"
    assert binding.explicit_base_url == "http://localhost:11434/v1"


def test_review_runtime_strips_blank_aux_credentials(cur):
    cfg = {"model": {"provider": "openrouter", "default": "openai/gpt-5.5"},
           "auxiliary": {"curator": {"provider": "openrouter", "model": "x/y", "api_key": "   ", "base_url": ""}}}
    binding = cur._resolve_review_runtime(cfg)
    assert binding.explicit_api_key is None and binding.explicit_base_url is None


def test_review_runtime_ignores_auxiliary_credentials_when_using_main(cur):
    cfg = {"model": {"provider": "openrouter", "default": "openai/gpt-5.5"},
           "auxiliary": {"curator": {"provider": "auto", "model": "", "api_key": "must-not-leak",
                                     "base_url": "http://curator-slot-ignored/"}}}
    binding = cur._resolve_review_runtime(cfg)
    assert (binding.provider, binding.model) == ("openrouter", "openai/gpt-5.5")
    assert binding.explicit_api_key is None and binding.explicit_base_url is None


def test_review_runtime_legacy_auxiliary_carry_credentials(cur, caplog):
    import logging
    cfg = {"model": {"provider": "openrouter", "default": "openai/gpt-5.5"},
           "curator": {"auxiliary": {"provider": "custom", "model": "m", "api_key": "legacy-key",
                                     "base_url": "http://legacy/v1"}}}
    with caplog.at_level(logging.INFO, logger="curator.curator"):
        binding = cur._resolve_review_runtime(cfg)
    assert binding.explicit_api_key == "legacy-key" and binding.explicit_base_url == "http://legacy/v1"
    assert any("deprecated curator.auxiliary" in rec.message for rec in caplog.records)


def test_review_model_auxiliary_curator_partial_override_falls_back(cur):
    base_main = {"provider": "openrouter", "default": "openai/gpt-5.5"}
    b = cur._resolve_review_runtime({"model": dict(base_main), "auxiliary": {"curator": {"provider": "openrouter", "model": ""}}})
    assert (b.provider, b.model) == ("openrouter", "openai/gpt-5.5")
    b = cur._resolve_review_runtime({"model": dict(base_main), "auxiliary": {"curator": {"provider": "auto", "model": "gpt-5.4-mini"}}})
    assert (b.provider, b.model) == ("openrouter", "openai/gpt-5.5")


def test_review_runtime_extra_body_merges(cur):
    cfg = {"auxiliary": {"curator": {"provider": "p", "model": "m", "extra_body": {"store": False}}}}
    assert cur._resolve_review_runtime(cfg).request_overrides == {"extra_body": {"store": False}}
    assert cur._merge_request_overrides({"extra_body": {"a": 1}}, {"b": 2}) == {"extra_body": {"a": 1, "b": 2}}


def test_curator_slot_is_canonical_aux_task(cur):
    from curator.config import DEFAULT_CONFIG
    slot = DEFAULT_CONFIG["auxiliary"]["curator"]
    assert slot["provider"] == "auto" and slot["model"] == "" and slot["timeout"] > 0


def test_review_prompt_prefers_archiving_duplicates_of_user_and_external_skills(cur):
    text = cur.CURATOR_REVIEW_PROMPT
    assert "ALREADY COVERED ELSEWHERE" in text and "absorbed_into=<that existing skill>" in text
    assert "owner=managed" in text and "never create an umbrella whose" in text
    assert text.index("Before building ANY umbrella") < text.index("Four ways to consolidate")
