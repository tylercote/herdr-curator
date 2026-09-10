"""curator.curator_backup — tar.gz snapshot + rollback of the skills tree."""

from __future__ import annotations

import json
import os
import tarfile
import tempfile
from pathlib import Path


def _write_skill(skills_dir: Path, name: str, body: str = "body") -> Path:
    d = skills_dir / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(f"---\nname: {name}\ndescription: t\nversion: 1.0\n---\n\n{body}\n", encoding="utf-8")
    return d


def test_snapshot_writes_tarball_and_manifest(home):
    from curator import curator_backup as cb
    _write_skill(home / "skills", "alpha")
    (home / "skills" / ".usage.json").write_text("{}", encoding="utf-8")
    snap = cb.snapshot_skills(reason="unit")
    assert snap is not None and (snap / "skills.tar.gz").exists()
    mf = json.loads((snap / "manifest.json").read_text(encoding="utf-8"))
    assert mf["reason"] == "unit" and mf["skill_files"] == 1 and mf["archive"] == "skills.tar.gz"
    assert mf["cron_jobs"]["backed_up"] is False and "reason" in mf["cron_jobs"]
    with tarfile.open(snap / "skills.tar.gz", "r:gz") as tf:
        names = tf.getnames()
    assert "alpha/SKILL.md" in names and ".usage.json" in names


def test_snapshot_disabled_by_config_and_missing_skills_dir(home, set_config):
    from curator import curator_backup as cb
    set_config({"curator": {"backup": {"enabled": False}}})
    assert cb.is_enabled() is False
    assert cb.snapshot_skills(reason="x") is None
    set_config({"curator": {"backup": {"enabled": True, "keep": "bogus"}}})
    assert cb.get_keep() == cb.DEFAULT_KEEP
    set_config({"curator": {"backup": {"keep": 0}}})
    assert cb.get_keep() == 1


def test_snapshot_uniquifies_when_same_second(home, monkeypatch):
    from curator import curator_backup as cb
    _write_skill(home / "skills", "alpha")
    frozen = "2026-05-01T12-00-00Z"
    monkeypatch.setattr(cb, "_utc_id", lambda now=None: frozen)
    s1 = cb.snapshot_skills(reason="a")
    s2 = cb.snapshot_skills(reason="b")
    assert s1.name == frozen and s2.name == f"{frozen}-01"


def test_snapshot_prunes_to_keep_count(home, monkeypatch):
    from curator import curator_backup as cb
    _write_skill(home / "skills", "alpha")
    monkeypatch.setattr(cb, "get_keep", lambda: 3)
    ids = [f"2026-05-0{i}T00-00-00Z" for i in range(1, 6)]
    for i, fid in enumerate(ids):
        monkeypatch.setattr(cb, "_utc_id", lambda now=None, _f=fid: _f)
        cb.snapshot_skills(reason=f"n{i}")
    remaining = sorted(p.name for p in (home / "skills" / ".curator_backups").iterdir())
    assert remaining == ids[2:]


def test_list_backups_and_resolve(home, monkeypatch):
    from curator import curator_backup as cb
    _write_skill(home / "skills", "alpha")
    monkeypatch.setattr(cb, "_utc_id", lambda now=None: "2026-05-01T00-00-00Z")
    cb.snapshot_skills(reason="one")
    monkeypatch.setattr(cb, "_utc_id", lambda now=None: "2026-05-02T00-00-00Z")
    cb.snapshot_skills(reason="two")
    rows = cb.list_backups()
    assert [r["id"] for r in rows] == ["2026-05-02T00-00-00Z", "2026-05-01T00-00-00Z"]
    assert rows[0]["reason"] == "two" and rows[0]["archive_bytes"] > 0
    assert cb._resolve_backup(None).name == "2026-05-02T00-00-00Z"
    assert cb._resolve_backup("2026-05-01T00-00-00Z").name == "2026-05-01T00-00-00Z"
    assert cb._resolve_backup("not-an-id") is None
    text = cb.summarize_backups()
    assert "2026-05-02T00-00-00Z" in text and "two" in text
    assert cb.summarize_backups.__doc__ or True
    (home / "skills" / ".curator_backups" / "2026-05-02T00-00-00Z" / "skills.tar.gz").unlink()
    assert cb._resolve_backup(None).name == "2026-05-01T00-00-00Z"


def test_summarize_backups_empty(home):
    from curator import curator_backup as cb
    assert cb.summarize_backups() == "No curator snapshots yet."


def test_rollback_is_itself_undoable(home):
    from curator import curator_backup as cb
    skills = home / "skills"
    _write_skill(skills, "v1")
    cb.snapshot_skills(reason="snapshot-of-v1")
    import shutil
    shutil.rmtree(skills / "v1")
    _write_skill(skills, "v2")
    ok, _, _ = cb.rollback()
    assert ok
    assert (skills / "v1").exists() and not (skills / "v2").exists()
    rows = cb.list_backups()
    assert any("pre-rollback" in (r.get("reason") or "") for r in rows)
    assert not list((skills / ".curator_backups").glob(".rollback-staging-*"))


def test_rollback_aborts_when_safety_snapshot_fails(home, monkeypatch):
    from curator import curator_backup as cb
    skills = home / "skills"
    skill_file = _write_skill(skills, "alpha", body="snapshot state") / "SKILL.md"
    target = cb.snapshot_skills(reason="rollback-target")
    skill_file.write_text("current state\n", encoding="utf-8")
    current = skill_file.read_bytes()
    monkeypatch.setattr(cb, "snapshot_skills", lambda *a, **k: None)
    ok, msg, restored = cb.rollback(target.name)
    assert not ok and "safety snapshot failed" in msg and restored is None
    assert skill_file.read_bytes() == current
    assert not list((skills / ".curator_backups").glob(".rollback-staging-*"))


def test_rollback_no_snapshots_returns_error(home):
    from curator import curator_backup as cb
    ok, msg, _ = cb.rollback()
    assert not ok and "no matching backup" in msg.lower()


def test_rollback_rejects_unsafe_tarball(home):
    from curator import curator_backup as cb
    skills = home / "skills"
    _write_skill(skills, "alpha")
    cb.snapshot_skills(reason="legit")
    snap_dir = Path(cb.list_backups()[0]["path"])
    mal = snap_dir / "skills.tar.gz"
    mal.unlink()
    with tarfile.open(mal, "w:gz") as tf:
        evil = tempfile.NamedTemporaryFile(delete=False, suffix=".md")
        evil.write(b"evil")
        evil.close()
        tf.add(evil.name, arcname="../../etc/evil.md")
        os.unlink(evil.name)
    ok, msg, _ = cb.rollback()
    assert not ok
    assert "unsafe" in msg.lower() or "refus" in msg.lower() or "extract" in msg.lower()


def test_real_run_takes_pre_snapshot(home, monkeypatch):
    from curator import curator, curator_backup as cb
    _write_skill(home / "skills", "alpha")
    monkeypatch.setattr(curator, "_run_llm_review", lambda p: {"final": "", "summary": "s", "model": "",
                                                                "provider": "", "tool_calls": [], "error": None})
    monkeypatch.setattr(curator, "apply_automatic_transitions",
                        lambda now=None: {"checked": 1, "marked_stale": 0, "archived": 0, "reactivated": 0})
    curator.run_curator_review(synchronous=True)
    assert any(r.get("reason") == "pre-curator-run" for r in cb.list_backups())


def test_dry_run_takes_no_snapshot(home, monkeypatch):
    from curator import curator, curator_backup as cb
    _write_skill(home / "skills", "alpha")
    curator.run_curator_review(synchronous=True, dry_run=True)
    assert cb.list_backups() == []


# ---------------------------------------------------------------------------
# cron-jobs backup + rollback
# ---------------------------------------------------------------------------

def _write_cron_jobs(home: Path, jobs: list) -> Path:
    cron_dir = home / "cron"
    cron_dir.mkdir(parents=True, exist_ok=True)
    path = cron_dir / "jobs.json"
    path.write_text(json.dumps({"jobs": jobs, "updated_at": "2026-05-01T00:00:00Z"}, indent=2), encoding="utf-8")
    return path


def test_snapshot_cron_jobs_utf8_bom_counted_and_backup_bomless(home):
    from curator import curator_backup as cb
    _write_skill(home / "skills", "alpha")
    cron_dir = home / "cron"
    cron_dir.mkdir()
    payload = json.dumps({"jobs": [{"id": "job-a"}, {"id": "job-b"}]})
    (cron_dir / "jobs.json").write_bytes(b"\xef\xbb\xbf" + payload.encode())
    snap = cb.snapshot_skills(reason="test")
    mf = json.loads((snap / "manifest.json").read_text(encoding="utf-8"))
    assert mf["cron_jobs"]["backed_up"] is True and mf["cron_jobs"]["jobs_count"] == 2
    assert "parse_warning" not in mf["cron_jobs"]
    backup_bytes = (snap / cb.CRON_JOBS_FILENAME).read_bytes()
    assert not backup_bytes.startswith(b"\xef\xbb\xbf")
    assert json.loads(backup_bytes) == json.loads(payload)


def test_rollback_restores_cron_skill_links(home):
    from curator import cron_jobs as cj, curator_backup as cb
    _write_skill(home / "skills", "alpha")
    _write_skill(home / "skills", "beta")
    _write_skill(home / "skills", "umbrella")
    cj.create_job(name="weekly", prompt="p", schedule="every 7d", skills=["alpha", "beta"])
    snap = cb.snapshot_skills(reason="pre-curator-run")
    cj.rewrite_skill_refs(consolidated={"alpha": "umbrella", "beta": "umbrella"}, pruned=[])
    assert cj.load_jobs()[0]["skills"] == ["umbrella"]
    ok, msg, _ = cb.rollback(backup_id=snap.name)
    assert ok, msg
    assert "cron links" in msg
    assert cj.load_jobs()[0]["skills"] == ["alpha", "beta"]


def test_rollback_leaves_new_jobs_untouched(home):
    from curator import cron_jobs as cj, curator_backup as cb
    _write_skill(home / "skills", "alpha")
    _write_cron_jobs(home, [{"id": "original", "name": "o", "schedule": "every 1h", "skills": ["alpha"]}])
    snap = cb.snapshot_skills(reason="pre-curator-run")
    jobs = cj.load_jobs()
    jobs.append({"id": "new-after-snapshot", "name": "new", "schedule": "every 15m", "skills": ["brand-new-skill"]})
    cj.save_jobs(jobs)
    ok, _, _ = cb.rollback(backup_id=snap.name)
    assert ok
    by_id = {j["id"]: j for j in cj.load_jobs()}
    assert by_id["new-after-snapshot"]["skills"] == ["brand-new-skill"]
    assert by_id["new-after-snapshot"]["schedule"] == "every 15m"


def test_restore_cron_skill_links_standalone(home):
    from curator import curator_backup as cb
    backups_dir = home / "skills" / ".curator_backups" / "fake-id"
    backups_dir.mkdir(parents=True)
    (backups_dir / cb.CRON_JOBS_FILENAME).write_text(json.dumps([
        {"id": "job-1", "name": "one", "skills": ["narrow-a", "narrow-b"]},
        {"id": "job-2", "name": "two", "skill": "legacy-single"},
        {"id": "job-gone", "name": "deleted", "skills": ["whatever"]}]), encoding="utf-8")
    _write_cron_jobs(home, [
        {"id": "job-1", "name": "one", "skills": ["umbrella"], "schedule": "every 1h"},
        {"id": "job-2", "name": "two", "skill": "legacy-single", "schedule": "every 1h"},
        {"id": "job-new", "name": "new", "skills": ["x"], "schedule": "every 1h"}])
    report = cb._restore_cron_skill_links(backups_dir)
    assert report["attempted"] is True and report["error"] is None
    assert report["unchanged"] == 1
    assert len(report["restored"]) == 1 and report["restored"][0]["job_id"] == "job-1"
    assert report["restored"][0]["to"]["skills"] == ["narrow-a", "narrow-b"]
    assert len(report["skipped_missing"]) == 1 and report["skipped_missing"][0]["job_id"] == "job-gone"


def test_rollback_safety_snapshot_never_prunes_its_target(home, monkeypatch):
    from curator import curator_backup as cb
    skills = home / "skills"
    monkeypatch.setattr(cb, "get_keep", lambda: 3)
    plan = [("2026-05-01T00-00-00Z", ["pristine"]), ("2026-05-02T00-00-00Z", ["pristine", "extra2"]),
            ("2026-05-03T00-00-00Z", ["pristine", "extra2", "extra3"])]
    for snap_id, names in plan:
        for n in names:
            _write_skill(skills, n)
        monkeypatch.setattr(cb, "_utc_id", lambda now=None, _i=snap_id: _i)
        assert cb.snapshot_skills(reason=snap_id) is not None
    monkeypatch.setattr(cb, "_utc_id", lambda now=None: "2026-05-09T00-00-00Z")
    ok, msg, _ = cb.rollback(backup_id="2026-05-01T00-00-00Z")
    assert ok, msg
    present = sorted(p.name for p in skills.iterdir() if p.name not in cb._EXCLUDE_TOP_LEVEL and not p.name.startswith("."))
    assert present == ["pristine"]


def test_rollback_recovers_cleanly_from_a_partial_extract(home, monkeypatch):
    from curator import curator_backup as cb
    skills = home / "skills"
    _write_skill(skills, "alpha", body="snapshot copy")
    assert cb.snapshot_skills(reason="before") is not None
    _write_skill(skills, "alpha", body="current copy")
    _write_skill(skills, "beta", body="current only")
    real_open = tarfile.open

    class _DiesMidExtract:
        def __init__(self, inner):
            self._inner = inner

        def getmembers(self):
            return self._inner.getmembers()

        def extractall(self, path, *args, **kwargs):
            partial = Path(path) / "alpha"
            partial.mkdir(parents=True, exist_ok=True)
            (partial / "SKILL.md").write_text("half written", encoding="utf-8")
            (Path(path) / "gamma").mkdir(parents=True, exist_ok=True)
            raise OSError(28, "No space left on device")

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            self._inner.close()
            return False

    def _open(name, mode="r", *args, **kwargs):
        handle = real_open(name, mode, *args, **kwargs)
        return _DiesMidExtract(handle) if mode.startswith("r") else handle

    monkeypatch.setattr(cb.tarfile, "open", _open)
    ok, msg, _ = cb.rollback()
    assert not ok
    assert not (skills / "alpha" / "alpha").exists()
    present = sorted(p.name for p in skills.iterdir() if p.name not in cb._EXCLUDE_TOP_LEVEL and not p.name.startswith("."))
    assert present == ["alpha", "beta"], f"tree not restored: {present}"
    assert "current copy" in (skills / "alpha" / "SKILL.md").read_text(encoding="utf-8")
    assert "current only" in (skills / "beta" / "SKILL.md").read_text(encoding="utf-8")


def test_snapshot_excludes_git_and_curator_backups_and_hub(home):
    from curator import curator_backup as cb
    skills = home / "skills"
    (skills / ".git").mkdir()
    (skills / ".git" / "config").write_text("[core]\n", encoding="utf-8")
    (skills / ".hub").mkdir()
    (skills / ".hub" / "lock.json").write_text("{}", encoding="utf-8")
    _write_skill(skills, "alpha", body="alpha body")
    nested_git = skills / "alpha" / ".git"
    nested_git.mkdir()
    (nested_git / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    snap_dir = cb.snapshot_skills(reason="test-exclude-git")
    with tarfile.open(snap_dir / "skills.tar.gz", "r:gz") as tf:
        members = tf.getnames()
    for name in members:
        parts = Path(name).parts
        assert ".git" not in parts and ".curator_backups" not in parts and ".hub" not in parts
    assert "alpha/SKILL.md" in members


def test_rollback_preserves_top_level_git(home):
    from curator import curator_backup as cb
    skills = home / "skills"
    git_dir = skills / ".git"
    git_dir.mkdir()
    (git_dir / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    _write_skill(skills, "alpha", body="v1")
    snap_dir = cb.snapshot_skills(reason="snap-v1")
    _write_skill(skills, "alpha", body="v2")
    _write_skill(skills, "beta", body="new")
    (git_dir / "HEAD").write_text("ref: refs/heads/feature\n", encoding="utf-8")
    ok, msg, _ = cb.rollback(backup_id=snap_dir.name)
    assert ok, msg
    assert "v1" in (skills / "alpha" / "SKILL.md").read_text(encoding="utf-8")
    assert not (skills / "beta").exists()
    assert (git_dir / "HEAD").read_text(encoding="utf-8") == "ref: refs/heads/feature\n"


def test_rollback_preserves_nested_git_inside_skill(home):
    from curator import curator_backup as cb
    skills = home / "skills"
    _write_skill(skills, "alpha", body="v1")
    nested_git = skills / "alpha" / ".git"
    nested_git.mkdir()
    (nested_git / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    snap_dir = cb.snapshot_skills(reason="snap-v1")
    _write_skill(skills, "alpha", body="v2")
    (nested_git / "HEAD").write_text("ref: refs/heads/feature\n", encoding="utf-8")
    ok, msg, _ = cb.rollback(backup_id=snap_dir.name)
    assert ok, msg
    assert "v1" in (skills / "alpha" / "SKILL.md").read_text(encoding="utf-8")
    assert (nested_git / "HEAD").read_text(encoding="utf-8") == "ref: refs/heads/feature\n"
    assert list((skills / ".curator_backups").glob(".rollback-staging-*")) == []


def test_rollback_preserves_nested_git_file_pointer(home):
    from curator import curator_backup as cb
    skills = home / "skills"
    _write_skill(skills, "alpha", body="v1")
    git_ptr = skills / "alpha" / ".git"
    git_ptr.write_text("gitdir: ../../.git/modules/alpha\n", encoding="utf-8")
    snap_dir = cb.snapshot_skills(reason="snap-v1")
    with tarfile.open(snap_dir / "skills.tar.gz", "r:gz") as tf:
        assert "alpha/.git" not in tf.getnames()
    _write_skill(skills, "alpha", body="v2")
    ok, msg, _ = cb.rollback(backup_id=snap_dir.name)
    assert ok, msg
    assert "v1" in (skills / "alpha" / "SKILL.md").read_text(encoding="utf-8")
    assert git_ptr.is_file() and git_ptr.read_text(encoding="utf-8").startswith("gitdir:")


def test_format_bytes():
    from curator.sizefmt import format_bytes
    assert format_bytes(10) == "10 B"
    assert format_bytes(1234567) == "1.2 MB"
    assert format_bytes(None) == "?"
    assert format_bytes(2 ** 40 * 3) == "3.0 TB"
