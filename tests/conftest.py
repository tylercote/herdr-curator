"""Shared fixtures.

Every module under ``curator/`` resolves its paths at CALL time through
``curator.paths`` (never at import), so an isolated home is just an env var:
no ``importlib.reload`` dance like the upstream Hermes tests need.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def write_skill(skills_dir: Path, name: str, category: str = "", body: str = "# body") -> Path:
    """Minimal SKILL.md whose frontmatter ``name:`` matches the directory name."""
    d = (skills_dir / category / name) if category else (skills_dir / name)
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: test skill\n---\n\n{body}\n", encoding="utf-8")
    return d


@pytest.fixture
def home(tmp_path, monkeypatch):
    """Isolated curator home: ``<tmp>/.hermes`` with an empty ``skills/``.

    Pins ``curator.prune_builtins`` OFF via config so bundled-protection tests
    exercise the off-path regardless of the shipped default; tests flip it
    with ``set_config``.
    """
    h = tmp_path / ".hermes"
    (h / "skills").mkdir(parents=True)
    monkeypatch.setenv("CURATOR_HOME", str(h))
    monkeypatch.delenv("HERMES_HOME", raising=False)
    monkeypatch.delenv("CURATOR_SKILLS_DIR", raising=False)
    monkeypatch.delenv("CURATOR_CONFIG", raising=False)
    monkeypatch.delenv("HERDR_PLUGIN_CONFIG_DIR", raising=False)
    monkeypatch.delenv("HERDR_PLUGIN_STATE_DIR", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    from curator import config as _config
    _config.clear_cache()
    (h / "config.json").write_text(json.dumps({"curator": {"prune_builtins": False}}), encoding="utf-8")
    _config.clear_cache()
    yield h
    _config.clear_cache()


@pytest.fixture
def skills(home):
    return home / "skills"


@pytest.fixture
def set_config(home):
    """Merge a partial config dict into the isolated home's config.json."""
    from curator import config as _config

    def _set(partial: dict) -> None:
        path = home / "config.json"
        current = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        _deep_merge(current, partial)
        path.write_text(json.dumps(current), encoding="utf-8")
        _config.clear_cache()
    return _set


def _deep_merge(dst: dict, src: dict) -> dict:
    for k, v in src.items():
        if isinstance(v, dict) and isinstance(dst.get(k), dict):
            _deep_merge(dst[k], v)
        else:
            dst[k] = v
    return dst


@pytest.fixture(autouse=True)
def _reset_contextvars():
    """Provenance/ledger-actor/read-mark ContextVars must never leak between tests."""
    yield
    try:
        from curator import skill_provenance, skill_ledger, skill_manager_guards
        skill_provenance._write_origin.set("foreground")
        skill_ledger._actor_override.set(None)
        skill_manager_guards._background_review_read_paths.set(None)
    except Exception:
        pass
