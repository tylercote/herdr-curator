"""Lifecycle hooks (mirrors ``hermes_cli.lifecycle.has_hook`` / ``invoke_hook``).

Hermes plugins can subscribe to ``on_skill_lifecycle``; ``skill_usage`` emits
``created / loaded / patched / edited / installed / stale / archived / restored``
facts through it. Here the registry is in-process (``register_hook``) — Herdr
has no Python plugin bus — but the emission contract is identical so the
telemetry code stays a verbatim port.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Dict, List

logger = logging.getLogger(__name__)

_HOOKS: Dict[str, List[Callable[..., Any]]] = {}


def register_hook(name: str, fn: Callable[..., Any]) -> None:
    _HOOKS.setdefault(name, []).append(fn)


def unregister_hook(name: str, fn: Callable[..., Any]) -> None:
    hooks = _HOOKS.get(name) or []
    if fn in hooks:
        hooks.remove(fn)


def has_hook(name: str) -> bool:
    return bool(_HOOKS.get(name))


def invoke_hook(name: str, **kwargs: Any) -> None:
    for fn in list(_HOOKS.get(name) or []):
        try:
            fn(**kwargs)
        except Exception:
            logger.debug("lifecycle hook %s failed", name, exc_info=True)
