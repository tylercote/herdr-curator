"""curator.paths + curator.config — home/skills-dir resolution and layered config.

Parity notes: Hermes reads ``~/.hermes/config.yaml`` (PyYAML). The plugin has no
third-party deps, so its primary config is ``config.json`` with the SAME key
tree; a ``config.yaml`` in the home is honoured only when PyYAML happens to be
importable (a real Hermes home).
"""

from __future__ import annotations

import json
from pathlib import Path


def test_home_prefers_curator_home_then_hermes_home_then_dot_hermes(tmp_path, monkeypatch):
    from curator import paths
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.delenv("CURATOR_HOME", raising=False)
    monkeypatch.delenv("HERMES_HOME", raising=False)
    assert paths.get_home() == tmp_path / ".hermes"
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "h"))
    assert paths.get_home() == tmp_path / "h"
    monkeypatch.setenv("CURATOR_HOME", str(tmp_path / "c"))
    assert paths.get_home() == tmp_path / "c"


def test_home_expands_tilde(tmp_path, monkeypatch):
    from curator import paths
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("CURATOR_HOME", "~/custom")
    assert paths.get_home() == tmp_path / "custom"


def test_skills_dir_defaults_under_home_and_honours_override(home, monkeypatch, set_config):
    from curator import paths
    assert paths.skills_dir() == home / "skills"
    set_config({"skills": {"dir": str(home / "elsewhere")}})
    assert paths.skills_dir() == home / "elsewhere"
    monkeypatch.setenv("CURATOR_SKILLS_DIR", str(home / "env-wins"))
    assert paths.skills_dir() == home / "env-wins"


def test_derived_paths_match_hermes_layout(home):
    """Hermes keeps sidecars INSIDE skills/ but blobs + logs under HOME. Preserve that split."""
    from curator import paths
    assert paths.usage_file() == home / "skills" / ".usage.json"
    assert paths.archive_dir() == home / "skills" / ".archive"
    assert paths.state_file() == home / "skills" / ".curator_state"
    assert paths.backups_dir() == home / "skills" / ".curator_backups"
    assert paths.ledger_path() == home / "skills" / ".curator_ledger.jsonl"
    assert paths.blobs_dir() == home / ".curator_backups" / "blobs"
    assert paths.reports_root() == home / "logs" / "curator"
    assert paths.cron_jobs_file() == home / "cron" / "jobs.json"


def test_config_defaults_match_hermes(home):
    from curator import config
    cfg = config.load_config()
    cur = cfg["curator"]
    assert cur["enabled"] is True
    assert cur["interval_hours"] == 24 * 7
    assert cur["min_idle_hours"] == 2
    assert cur["stale_after_days"] == 30
    assert cur["archive_after_days"] == 90
    assert cur["consolidate"] is False
    assert cur["archive_ttl_days"] == 0
    assert cur["backup"] == {"enabled": True, "keep": 5}
    assert cfg["skills"]["ledger"] is True
    assert cfg["skills"]["external_dirs"] == []
    assert cfg["skills"]["create_dir"] == ""
    slot = cfg["auxiliary"]["curator"]
    assert slot["provider"] == "auto" and slot["model"] == "" and slot["timeout"] > 0


def test_config_json_overrides_defaults_deep_merge(home, set_config):
    from curator import config
    set_config({"curator": {"stale_after_days": 7, "backup": {"keep": 2}}})
    cfg = config.load_config()
    assert cfg["curator"]["stale_after_days"] == 7
    assert cfg["curator"]["backup"] == {"enabled": True, "keep": 2}
    assert cfg["curator"]["archive_after_days"] == 90  # untouched sibling survives


def test_config_path_resolution_order(home, monkeypatch, tmp_path):
    from curator import config
    assert config.config_path() == home / "config.json"
    plugin_cfg = tmp_path / "plugcfg"
    plugin_cfg.mkdir()
    monkeypatch.setenv("HERDR_PLUGIN_CONFIG_DIR", str(plugin_cfg))
    config.clear_cache()
    assert config.config_path() == plugin_cfg / "config.json"
    monkeypatch.setenv("CURATOR_CONFIG", str(tmp_path / "explicit.json"))
    config.clear_cache()
    assert config.config_path() == tmp_path / "explicit.json"


def test_corrupt_config_json_falls_back_to_defaults(home):
    from curator import config
    (home / "config.json").write_text("{ nope", encoding="utf-8")
    config.clear_cache()
    assert config.load_config()["curator"]["enabled"] is True


def test_cfg_get_semantics(home):
    from curator.config import cfg_get
    cfg = {"a": {"b": None, "c": 1}}
    assert cfg_get(cfg, "a", "c") == 1
    assert cfg_get(cfg, "a", "b", default="d") is None  # explicit None returned as-is
    assert cfg_get(cfg, "a", "zzz", default="d") == "d"
    assert cfg_get(None, "a", default=3) == 3
    assert cfg_get(cfg, "a", "c", "deeper", default="d") == "d"


def test_load_config_returns_deepcopy_but_readonly_is_cached(home):
    from curator import config
    a = config.load_config()
    a["curator"]["enabled"] = "mutated"
    assert config.load_config()["curator"]["enabled"] is True
    r1, r2 = config.load_config_readonly(), config.load_config_readonly()
    assert r1 is r2


def test_config_cache_invalidates_on_file_change(home, set_config):
    from curator import config
    assert config.load_config()["curator"]["stale_after_days"] == 30
    set_config({"curator": {"stale_after_days": 3}})
    assert config.load_config()["curator"]["stale_after_days"] == 3


def test_expand_path_handles_tilde_env_and_home_relative(home, monkeypatch, tmp_path):
    from curator import paths
    monkeypatch.setenv("CURATOR_TEST_DIR", str(tmp_path / "envdir"))
    assert paths.expand_path("~/x") == tmp_path / "x"
    assert paths.expand_path("${CURATOR_TEST_DIR}/y") == tmp_path / "envdir" / "y"
    # relative entries are resolved relative to HOME (Hermes: relative to HERMES_HOME)
    assert paths.expand_path("rel/z") == home / "rel" / "z"
