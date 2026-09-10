"""Layered configuration.

A single ``config.json`` deep-merged over ``DEFAULT_CONFIG``. Precedence for
which file that is, highest first:

    CURATOR_CONFIG
    $HERDR_PLUGIN_CONFIG_DIR/config.json          (set by Herdr for manifest commands)
    <home>/config.json                            (explicit CURATOR_HOME only)
    ${XDG_CONFIG_HOME:-~/.config}/herdr/plugins/config/curator/config.json

Cached on the file signature ``(mtime_ns, size)`` so a read is cheap and an
edit is picked up without restart.
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
_CACHE: Dict[str, Tuple[Optional[Tuple[int, int]], Dict[str, Any]]] = {}


def config_path() -> Path:
    explicit = os.environ.get("CURATOR_CONFIG")
    if explicit:
        return paths.expanduser(explicit)
    if os.environ.get("HERDR_PLUGIN_CONFIG_DIR"):
        return paths.herdr_config_dir() / "config.json"
    if paths.explicit_home():
        return paths.get_home() / "config.json"
    return paths.herdr_config_dir() / "config.json"


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


def _build() -> Dict[str, Any]:
    merged = copy.deepcopy(DEFAULT_CONFIG)
    jpath = config_path()
    if jpath.exists():
        _deep_merge(merged, _read_json(jpath))
    return merged


def load_config_readonly() -> Dict[str, Any]:
    """Cached merged config. Never mutate the result — use ``load_config`` for that."""
    jpath = config_path()
    key = str(jpath)
    sig = _signature(jpath)
    with _LOCK:
        cached = _CACHE.get(key)
        if cached is not None and cached[0] == sig:
            return cached[1]
        built = _build()
        _CACHE[key] = (sig, built)
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


def read_user_config() -> Dict[str, Any]:
    """The raw ``config.json`` layer only (no defaults), ``{}`` when missing."""
    path = config_path()
    return _read_json(path) if path.exists() else {}


def update_user_config(mutator) -> Dict[str, Any]:
    """Load ``config.json``, apply ``mutator(cfg)`` in place, write it back atomically.
    Only the user layer is written — defaults are never frozen into the file."""
    from curator.fsutil import atomic_json_write
    cfg = read_user_config()
    mutator(cfg)
    atomic_json_write(config_path(), cfg, indent=2, sort_keys=True)
    clear_cache()
    return cfg


def read_config_section(*path: str) -> Dict[str, Any]:
    """Nested section as a dict; ``{}`` when missing or not a dict."""
    node: Any = load_config_readonly()
    for key in path:
        node = node.get(key) if isinstance(node, dict) else None
    return node if isinstance(node, dict) else {}
