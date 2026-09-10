"""curator.paths + curator.config — home/skills-dir resolution and layered config.

An explicit ``CURATOR_HOME`` is a self-contained tree (the ``home`` fixture);
without one the plugin uses Herdr's plugin dirs and Claude Code's skills
(the ``herdr_layout`` fixture).
"""

from __future__ import annotations

import json
from pathlib import Path


def test_home_prefers_curator_home_then_herdr_state_dir_then_xdg(herdr_layout, monkeypatch):
    from curator import paths
    tmp = herdr_layout
    assert paths.get_home() == tmp / "xdg-state" / "herdr" / "plugins" / "curator"
    monkeypatch.delenv("XDG_STATE_HOME")
    assert paths.get_home() == tmp / ".local" / "state" / "herdr" / "plugins" / "curator"
    monkeypatch.setenv("HERDR_PLUGIN_STATE_DIR", str(tmp / "state"))
    assert paths.get_home() == tmp / "state"
    monkeypatch.setenv("CURATOR_HOME", str(tmp / "c"))
    assert paths.get_home() == tmp / "c"


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


def test_skills_dir_defaults_to_claude_tree_under_herdr(herdr_layout):
    from curator import paths
    assert paths.skills_dir() == herdr_layout / ".claude" / "skills"
    assert paths.display_skills_dir() == "~/.claude/skills"


def test_explicit_home_is_self_contained(tmp_path, monkeypatch):
    """CURATOR_HOME keeps skills, config and plugin state under itself — never Claude's tree."""
    from curator import config, paths
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    for var in ("CURATOR_SKILLS_DIR", "CURATOR_CONFIG", "HERDR_PLUGIN_CONFIG_DIR", "HERDR_PLUGIN_STATE_DIR"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("CURATOR_HOME", str(tmp_path / "elsewhere"))
    config.clear_cache()
    assert paths.skills_dir() == tmp_path / "elsewhere" / "skills"
    assert config.config_path() == tmp_path / "elsewhere" / "config.json"
    assert paths.plugin_state_dir() == tmp_path / "elsewhere" / ".curator_plugin"
    config.clear_cache()


def test_bare_cli_and_plugin_resolve_identical_paths(herdr_layout, monkeypatch):
    """What Herdr passes in HERDR_PLUGIN_* must equal what the XDG fallback reconstructs,
    so `curator` typed in any shell and the scheduled plugin run agree on every path."""
    from curator import config, paths
    tmp = herdr_layout
    bare = (paths.get_home(), config.config_path(), paths.skills_dir(), paths.plugin_state_dir())
    monkeypatch.setenv("HERDR_PLUGIN_STATE_DIR", str(tmp / "xdg-state" / "herdr" / "plugins" / "curator"))
    monkeypatch.setenv("HERDR_PLUGIN_CONFIG_DIR", str(tmp / "xdg-config" / "herdr" / "plugins" / "config" / "curator"))
    config.clear_cache()
    assert (paths.get_home(), config.config_path(), paths.skills_dir(), paths.plugin_state_dir()) == bare
    assert paths.plugin_state_dir() == paths.get_home()


def test_plugin_config_is_read_without_herdr_layout(herdr_layout):
    """The plugin's config.json governs a bare `curator` too (consolidate etc. must not silently flip)."""
    import json
    from curator import config, paths
    cfg_dir = herdr_layout / "xdg-config" / "herdr" / "plugins" / "config" / "curator"
    cfg_dir.mkdir(parents=True)
    (cfg_dir / "config.json").write_text(json.dumps({"skills": {"dir": "~/work/skills"}, "curator": {"consolidate": True}}))
    config.clear_cache()
    assert config.load_config_readonly()["curator"]["consolidate"] is True
    assert paths.skills_dir() == herdr_layout / "work" / "skills"


def test_derived_paths_layout(home):
    """Sidecars live INSIDE skills/ but blobs + logs under HOME. Preserve that split."""
    from curator import paths
    assert paths.usage_file() == home / "skills" / ".usage.json"
    assert paths.archive_dir() == home / "skills" / ".archive"
    assert paths.state_file() == home / "skills" / ".curator_state"
    assert paths.backups_dir() == home / "skills" / ".curator_backups"
    assert paths.ledger_path() == home / "skills" / ".curator_ledger.jsonl"
    assert paths.blobs_dir() == home / ".curator_backups" / "blobs"
    assert paths.reports_root() == home / "logs" / "curator"
    assert paths.cron_jobs_file() == home / "cron" / "jobs.json"


def test_config_defaults(home):
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
    # relative entries are resolved relative to HOME
    assert paths.expand_path("rel/z") == home / "rel" / "z"
