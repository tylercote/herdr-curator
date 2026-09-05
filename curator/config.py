"""Layered configuration (mirrors ``hermes_cli.config.load_config`` / ``cfg_get``
and the curator-relevant slice of ``hermes_cli/config_defaults.py``).

Hermes reads ``~/.hermes/config.yaml`` via PyYAML. This plugin carries no
third-party dependency, so its primary file is ``config.json`` with the SAME
key tree. Precedence, lowest to highest:

    DEFAULT_CONFIG
    <home>/config.yaml      (only if PyYAML is importable — a real Hermes home)
    config.json             (CURATOR_CONFIG > $HERDR_PLUGIN_CONFIG_DIR/config.json > <home>/config.json)

Deep-merged, cached on the file signature ``(mtime_ns, size)`` exactly like
Hermes so a read is cheap and an edit is picked up without restart.
"""

from __future__ import annotations

import copy
import json
import logging
import os
import threading
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from curator import paths

logger = logging.getLogger(__name__)


def _aux(timeout: int, reasoning_effort: bool = True) -> Dict[str, Any]:
    slot: Dict[str, Any] = {"provider": "auto", "model": "", "base_url": "", "api_key": "", "timeout": timeout,
                            "extra_body": {}}
    if reasoning_effort:
        slot["reasoning_effort"] = ""
    return slot


DEFAULT_CONFIG: Dict[str, Any] = {
    # Main chat model — the fallback for every "auto" auxiliary slot.
    "model": {"provider": "auto", "default": ""},
    "auxiliary": {
        # Curator skill-usage review can take minutes on reasoning models.
        "curator": _aux(600),
    },
    "skills": {
        "dir": "",                 # plugin-only: override the skills tree location
        "external_dirs": [],
        "create_dir": "",
        "project_discovery": True,
        "trusted_project_dirs": [],
        "disabled": [],
        "platform_disabled": {},
        # Audit ledger: every skill mutation appends to <skills>/.curator_ledger.jsonl.
        "ledger": True,
    },
    "curator": {
        "enabled": True,
        "interval_hours": 24 * 7,
        "min_idle_hours": 2,
        "stale_after_days": 30,
        "archive_after_days": 90,
        "consolidate": False,
        "prune_builtins": True,
        "archive_ttl_days": 0,
        "backup": {"enabled": True, "keep": 5},
    },
}

_LOCK = threading.RLock()
_CACHE: Dict[str, Tuple[Optional[Tuple[int, int]], Optional[Tuple[int, int]], Dict[str, Any]]] = {}


def config_path() -> Path:
    explicit = os.environ.get("CURATOR_CONFIG")
    if explicit:
        return paths.expanduser(explicit)
    plugin_dir = os.environ.get("HERDR_PLUGIN_CONFIG_DIR")
    if plugin_dir:
        return Path(plugin_dir) / "config.json"
    return paths.get_home() / "config.json"


def yaml_config_path() -> Path:
    return paths.get_home() / "config.yaml"


def clear_cache() -> None:
    with _LOCK:
        _CACHE.clear()


def _signature(path: Path) -> Optional[Tuple[int, int]]:
    try:
        st = path.stat()
    except OSError:
        return None
    return (st.st_mtime_ns, st.st_size)


def _deep_merge(dst: Dict[str, Any], src: Dict[str, Any]) -> Dict[str, Any]:
    for key, value in src.items():
        if isinstance(value, dict) and isinstance(dst.get(key), dict):
            _deep_merge(dst[key], value)
        else:
            dst[key] = copy.deepcopy(value)
    return dst


def _read_json(path: Path) -> Dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as e:
        logger.debug("config: ignoring %s: %s", path, e)
        return {}
    return data if isinstance(data, dict) else {}


def _read_yaml(path: Path) -> Dict[str, Any]:
    try:
        import yaml  # type: ignore
    except ImportError:
        return {}
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8-sig"))
    except Exception as e:
        logger.debug("config: ignoring %s: %s", path, e)
        return {}
    return data if isinstance(data, dict) else {}


def _build() -> Dict[str, Any]:
    merged = copy.deepcopy(DEFAULT_CONFIG)
    ypath, jpath = yaml_config_path(), config_path()
    if ypath.exists():
        _deep_merge(merged, _read_yaml(ypath))
    if jpath.exists():
        _deep_merge(merged, _read_json(jpath))
    return merged


def load_config_readonly() -> Dict[str, Any]:
    """Cached merged config. Never mutate the result — use ``load_config`` for that."""
    jpath, ypath = config_path(), yaml_config_path()
    key = f"{jpath}|{ypath}"
    sig = (_signature(jpath), _signature(ypath))
    with _LOCK:
        cached = _CACHE.get(key)
        if cached is not None and cached[:2] == sig:
            return cached[2]
        built = _build()
        _CACHE[key] = (sig[0], sig[1], built)
        return built


def load_config() -> Dict[str, Any]:
    return copy.deepcopy(load_config_readonly())


def cfg_get(cfg: Optional[Dict[str, Any]], *keys: str, default: Any = None) -> Any:
    """Traverse nested keys; ``default`` only when a key is ABSENT (explicit None passes through)."""
    if not isinstance(cfg, dict):
        return default
    node: Any = cfg
    for key in keys:
        if not isinstance(node, dict) or key not in node:
            return default
        node = node[key]
    return node


def read_config_section(*path: str) -> Dict[str, Any]:
    """Nested section as a dict; ``{}`` when missing or not a dict."""
    node: Any = load_config_readonly()
    for key in path:
        node = node.get(key) if isinstance(node, dict) else None
    return node if isinstance(node, dict) else {}
