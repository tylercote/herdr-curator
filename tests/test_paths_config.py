"""curator.paths + curator.config — state/skills-dir resolution, the per-tree layout
and layered config. ``home`` pins state, skills and config to one tmp dir via env;
``herdr_layout`` is a plain Herdr install with HOME and the XDG roots redirected.
"""

from __future__ import annotations

import json
import os
from pathlib import Path


def test_state_dir_prefers_herdr_then_curator_home_then_xdg(herdr_layout, monkeypatch):
    from curator import paths
    tmp = herdr_layout
    assert paths.state_dir() == tmp / "xdg-state" / "herdr" / "plugins" / "curator"
    monkeypatch.delenv("XDG_STATE_HOME")
    assert paths.state_dir() == tmp / ".local" / "state" / "herdr" / "plugins" / "curator"
    monkeypatch.setenv("CURATOR_HOME", str(tmp / "c"))
    assert paths.state_dir() == tmp / "c"
    monkeypatch.setenv("HERDR_PLUGIN_STATE_DIR", str(tmp / "state"))
    assert paths.state_dir() == tmp / "state"  # Herdr's dir wins over the shell override


def test_curator_home_expands_tilde(tmp_path, monkeypatch):
    from curator import paths
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.delenv("HERDR_PLUGIN_STATE_DIR", raising=False)
    monkeypatch.setenv("CURATOR_HOME", "~/custom")
    assert paths.state_dir() == tmp_path / "custom"


def test_skills_dir_env_beats_config_beats_default(herdr_layout, monkeypatch):
    from curator import config, paths
    tmp = herdr_layout
    assert paths.skills_dir() == tmp / ".claude" / "skills"
    assert paths.display_skills_dir() == "~/.claude/skills"
    cfg_dir = tmp / "xdg-config" / "herdr" / "plugins" / "config" / "curator"
    cfg_dir.mkdir(parents=True)
    (cfg_dir / "config.json").write_text(json.dumps({"skills": {"dir": str(tmp / "elsewhere")}}))
    config.clear_cache()
    assert paths.skills_dir() == tmp / "elsewhere"
    monkeypatch.setenv("CURATOR_SKILLS_DIR", str(tmp / "env-wins"))
    assert paths.skills_dir() == tmp / "env-wins"


def test_curator_home_never_repoints_skills_or_config(tmp_path, monkeypatch):
    """CURATOR_HOME is only the state dir: skills stay Claude Code's and config stays Herdr's."""
    from curator import config, paths
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    for var in ("CURATOR_SKILLS_DIR", "CURATOR_CONFIG", "HERDR_PLUGIN_CONFIG_DIR", "HERDR_PLUGIN_STATE_DIR"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg-config"))
    monkeypatch.setenv("CURATOR_HOME", str(tmp_path / "elsewhere"))
    config.clear_cache()
    assert paths.state_dir() == tmp_path / "elsewhere"
    assert paths.skills_dir() == tmp_path / ".claude" / "skills"
    assert config.config_path() == tmp_path / "xdg-config" / "herdr" / "plugins" / "config" / "curator" / "config.json"
    config.clear_cache()


def test_bare_cli_and_plugin_resolve_identical_paths(herdr_layout, monkeypatch):
    """What Herdr passes in HERDR_PLUGIN_* must equal what the XDG fallback reconstructs,
    so `curator` typed in any shell and the scheduled plugin run agree on every path."""
    from curator import config, paths
    tmp = herdr_layout
    bare = (paths.state_dir(), config.config_path(), paths.skills_dir(), paths.tree_dir())
    monkeypatch.setenv("HERDR_PLUGIN_STATE_DIR", str(tmp / "xdg-state" / "herdr" / "plugins" / "curator"))
    monkeypatch.setenv("HERDR_PLUGIN_CONFIG_DIR", str(tmp / "xdg-config" / "herdr" / "plugins" / "config" / "curator"))
    config.clear_cache()
    assert (paths.state_dir(), config.config_path(), paths.skills_dir(), paths.tree_dir()) == bare


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


def test_derived_paths_live_under_one_per_tree_dir_outside_the_skills_tree(home):
    """The skills tree gets NOTHING from the curator; everything sits under <state>/trees/<key>/."""
    from curator import paths
    tree = paths.tree_dir()
    assert tree.parent == home / "trees" and tree.name == paths.tree_key(home / "skills")
    assert paths.usage_file() == tree / "usage.json"
    assert paths.state_file() == tree / "state.json"
    assert paths.ledger_path() == tree / "ledger.jsonl"
    assert paths.blobs_dir() == tree / "blobs"
    assert paths.archive_dir() == tree / "archive"
    assert paths.backups_dir() == tree / "snapshots"
    assert paths.reports_root() == home / "logs" / "curator"
    for p in (paths.usage_file(), paths.state_file(), paths.ledger_path(), paths.blobs_dir(), paths.archive_dir(), paths.backups_dir()):
        assert not str(p).startswith(str(home / "skills"))
    assert not hasattr(paths, "cron_jobs_file")


def test_tree_key_is_readable_stable_and_follows_symlinks(tmp_path, monkeypatch):
    from curator import paths
    real = tmp_path / ".claude" / "skills"
    real.mkdir(parents=True)
    key = paths.tree_key(real)
    assert key.startswith("claude-skills-") and len(key) == len("claude-skills-") + 8
    assert paths.tree_key(real) == key
    link = tmp_path / "linked-skills"
    link.symlink_to(real)
    assert paths.tree_key(link) == key
    assert paths.tree_key(tmp_path / "other") != key
    monkeypatch.setenv("CURATOR_SKILLS_DIR", str(link))
    monkeypatch.setenv("CURATOR_HOME", str(tmp_path / "state"))
    monkeypatch.delenv("HERDR_PLUGIN_STATE_DIR", raising=False)
    assert paths.tree_dir() == tmp_path / "state" / "trees" / key


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


def test_config_path_resolution_order(herdr_layout, monkeypatch, tmp_path):
    from curator import config
    assert config.config_path() == herdr_layout / "xdg-config" / "herdr" / "plugins" / "config" / "curator" / "config.json"
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
