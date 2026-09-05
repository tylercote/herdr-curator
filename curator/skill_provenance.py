"""Skill write-origin provenance (verbatim port of ``tools/skill_provenance.py``).

A ContextVar separating background-review (curator fork) skill writes from
foreground user-directed writes. The curator only curates skills the review
fork created; skills a user asked for belong to the user. The MCP server that
fronts the consolidation pass binds ``BACKGROUND_REVIEW`` for the life of the
process; everything else defaults to ``"foreground"``.
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
