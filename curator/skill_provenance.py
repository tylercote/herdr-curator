"""Skill write-origin provenance.

A ContextVar separating background-review (curator fork) skill writes from
foreground writes. The origin is telemetry and behaviour flavour only: ledger
``actor`` (curator vs agent), ``skill_view`` counting a view rather than a
use, delete archiving instead of removing, and the read-before-write and
consolidation-delete guards. It never decides whether ownership is checked —
``skill_manager_guards._ownership_write_guard`` refuses non-managed, external
and pinned skills under every origin. ``mcp_server.Server`` binds
``BACKGROUND_REVIEW`` for the life of the process; the default is ``"foreground"``.
"""

from __future__ import annotations

import contextvars

_write_origin: contextvars.ContextVar = contextvars.ContextVar("skill_write_origin", default="foreground")
BACKGROUND_REVIEW = "background_review"


def set_current_write_origin(origin: str) -> contextvars.Token:
    return _write_origin.set(origin or "foreground")


def reset_current_write_origin(token: contextvars.Token) -> None:
    _write_origin.reset(token)


def get_current_write_origin() -> str:
    return _write_origin.get()


def is_background_review() -> bool:
    return get_current_write_origin() == BACKGROUND_REVIEW
