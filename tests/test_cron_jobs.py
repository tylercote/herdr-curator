"""curator.cron_jobs — the ``cron/jobs.py`` subset the curator depends on.

Hermes keeps scheduled jobs in ``~/.hermes/cron/jobs.json``; the curator reads
skill references from it (protection) and rewrites them after consolidation.
Port of ``tests/cron/test_rewrite_skill_refs.py`` plus reference canonicalisation.
"""

from __future__ import annotations

import json

from conftest import write_skill


class TestRewriteSkillRefsNoop:
    def test_empty_map_and_no_jobs(self, home):
        from curator.cron_jobs import rewrite_skill_refs
        assert rewrite_skill_refs(consolidated={}, pruned=[]) == {"rewrites": [], "jobs_updated": 0, "jobs_scanned": 0}

    def test_jobs_exist_but_map_empty(self, home):
        from curator.cron_jobs import create_job, rewrite_skill_refs
        create_job(prompt="", schedule="every 1h", skills=["foo"])
        report = rewrite_skill_refs(consolidated={}, pruned=[])
        assert report["jobs_updated"] == 0 and report["jobs_scanned"] == 0


class TestRewriteSkillRefsConsolidation:
    def test_single_skill_replaced(self, home):
        from curator.cron_jobs import create_job, get_job, rewrite_skill_refs
        job = create_job(prompt="", schedule="every 1h", skills=["legacy-skill"])
        report = rewrite_skill_refs(consolidated={"legacy-skill": "umbrella-skill"}, pruned=[])
        assert report["jobs_updated"] == 1
        loaded = get_job(job["id"])
        assert loaded["skills"] == ["umbrella-skill"] and loaded["skill"] == "umbrella-skill"

    def test_multiple_skills_one_consolidated(self, home):
        from curator.cron_jobs import create_job, get_job, rewrite_skill_refs
        job = create_job(prompt="", schedule="every 1h", skills=["keep-a", "legacy", "keep-b"])
        rewrite_skill_refs(consolidated={"legacy": "umbrella"}, pruned=[])
        assert get_job(job["id"])["skills"] == ["keep-a", "umbrella", "keep-b"]

    def test_umbrella_already_in_list_dedupes(self, home):
        from curator.cron_jobs import create_job, get_job, rewrite_skill_refs
        job = create_job(prompt="", schedule="every 1h", skills=["umbrella", "legacy"])
        rewrite_skill_refs(consolidated={"legacy": "umbrella"}, pruned=[])
        assert get_job(job["id"])["skills"] == ["umbrella"]

    def test_rewrite_report_records_mapping(self, home):
        from curator.cron_jobs import create_job, rewrite_skill_refs
        job = create_job(prompt="", schedule="every 1h", skills=["a", "b"], name="my-job")
        report = rewrite_skill_refs(consolidated={"a": "umbrella-a", "b": "umbrella-b"}, pruned=[])
        entry = report["rewrites"][0]
        assert entry["job_id"] == job["id"] and entry["job_name"] == "my-job"
        assert entry["before"] == ["a", "b"] and entry["after"] == ["umbrella-a", "umbrella-b"]
        assert entry["mapped"] == {"a": "umbrella-a", "b": "umbrella-b"} and entry["dropped"] == []


class TestRewriteSkillRefsPruning:
    def test_pruned_skill_dropped(self, home):
        from curator.cron_jobs import create_job, get_job, rewrite_skill_refs
        job = create_job(prompt="", schedule="every 1h", skills=["keep", "stale"])
        report = rewrite_skill_refs(consolidated={}, pruned=["stale"])
        assert report["jobs_updated"] == 1
        loaded = get_job(job["id"])
        assert loaded["skills"] == ["keep"] and loaded["skill"] == "keep"

    def test_all_skills_pruned_leaves_empty_list(self, home):
        from curator.cron_jobs import create_job, get_job, rewrite_skill_refs
        job = create_job(prompt="", schedule="every 1h", skills=["gone"])
        rewrite_skill_refs(consolidated={}, pruned=["gone"])
        loaded = get_job(job["id"])
        assert loaded["skills"] == [] and loaded["skill"] is None


class TestRewriteSkillRefsMixed:
    def test_mixed_consolidation_and_pruning(self, home):
        from curator.cron_jobs import create_job, get_job, rewrite_skill_refs
        job = create_job(prompt="", schedule="every 1h", skills=["keep", "legacy", "stale"])
        rewrite_skill_refs(consolidated={"legacy": "umbrella"}, pruned=["stale"])
        assert get_job(job["id"])["skills"] == ["keep", "umbrella"]

    def test_skill_in_both_maps_wins_as_consolidated(self, home):
        from curator.cron_jobs import create_job, get_job, rewrite_skill_refs
        job = create_job(prompt="", schedule="every 1h", skills=["ambiguous"])
        rewrite_skill_refs(consolidated={"ambiguous": "umbrella"}, pruned=["ambiguous"])
        assert get_job(job["id"])["skills"] == ["umbrella"]


class TestRewriteSkillRefsMultipleJobs:
    def test_only_affected_jobs_reported(self, home):
        from curator.cron_jobs import create_job, get_job, rewrite_skill_refs
        j1 = create_job(prompt="", schedule="every 1h", skills=["legacy"])
        j2 = create_job(prompt="", schedule="every 1h", skills=["untouched"])
        j3 = create_job(prompt="no skills here", schedule="every 1h", skills=[])
        report = rewrite_skill_refs(consolidated={"legacy": "umbrella"}, pruned=[])
        assert report["jobs_updated"] == 1 and report["jobs_scanned"] == 3
        assert report["rewrites"][0]["job_id"] == j1["id"]
        assert get_job(j2["id"])["skills"] == ["untouched"]
        assert get_job(j3["id"])["skills"] == []

    def test_legacy_skill_field_also_rewritten(self, home):
        from curator.cron_jobs import create_job, get_job, rewrite_skill_refs
        job = create_job(prompt="", schedule="every 1h", skill="legacy")
        rewrite_skill_refs(consolidated={"legacy": "umbrella"}, pruned=[])
        loaded = get_job(job["id"])
        assert loaded["skills"] == ["umbrella"] and loaded["skill"] == "umbrella"


class TestRewriteSkillRefsPersistence:
    def test_changes_persist_across_reload(self, home):
        from curator.cron_jobs import create_job, jobs_file, rewrite_skill_refs
        create_job(prompt="", schedule="every 1h", skills=["legacy"])
        rewrite_skill_refs(consolidated={"legacy": "umbrella"}, pruned=[])
        data = json.loads(jobs_file().read_text(encoding="utf-8"))
        assert data["jobs"][0]["skills"] == ["umbrella"] and data["jobs"][0]["skill"] == "umbrella"

    def test_noop_does_not_rewrite_file(self, home):
        from curator.cron_jobs import create_job, jobs_file, rewrite_skill_refs
        create_job(prompt="", schedule="every 1h", skills=["keep"])
        mtime_before = jobs_file().stat().st_mtime_ns
        report = rewrite_skill_refs(consolidated={"unrelated": "umbrella"}, pruned=["other"])
        assert report["jobs_updated"] == 0
        assert jobs_file().stat().st_mtime_ns == mtime_before


class TestReferencedSkillNames:
    def test_includes_paused_and_legacy_single_field(self, home):
        from curator.cron_jobs import referenced_skill_names, save_jobs
        save_jobs([{"id": "a", "enabled": False, "skills": ["x", "y"]}, {"id": "b", "skill": "z"}, "junk"])
        assert referenced_skill_names() == {"x", "y", "z"}

    def test_canonicalizes_absolute_paths(self, home):
        from curator.cron_jobs import referenced_skill_names, save_jobs
        write_skill(home / "skills", "quarterly-report")
        save_jobs([{"id": "a", "skills": [str(home / "skills" / "quarterly-report")]}])
        assert referenced_skill_names() == {"quarterly-report"}

    def test_unresolvable_reference_is_kept_verbatim(self, home, tmp_path):
        from curator.cron_jobs import referenced_skill_names, save_jobs
        outside = tmp_path / "elsewhere" / "some-skill"
        save_jobs([{"id": "a", "skills": [str(outside)]}])
        assert referenced_skill_names() == {str(outside).strip().lstrip("/")}

    def test_corrupt_store_yields_empty_set(self, home):
        from curator.cron_jobs import jobs_file, referenced_skill_names
        jobs_file().parent.mkdir(parents=True, exist_ok=True)
        jobs_file().write_text("{ nope", encoding="utf-8")
        assert referenced_skill_names() == set()


def test_load_jobs_accepts_bare_list_and_bom(home):
    from curator.cron_jobs import jobs_file, load_jobs
    jobs_file().parent.mkdir(parents=True, exist_ok=True)
    jobs_file().write_bytes(b"\xef\xbb\xbf" + json.dumps([{"id": "j", "skills": ["s"]}]).encode())
    assert load_jobs() == [{"id": "j", "skills": ["s"]}]
    assert json.loads(jobs_file().read_text(encoding="utf-8"))["jobs"][0]["id"] == "j"  # auto-repaired
