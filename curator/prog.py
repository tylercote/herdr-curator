"""The user-facing program name used in hints (Hermes prints ``hermes curator <verb>``;
this plugin's CLI is ``curator <verb>``). Override with ``CURATOR_PROG`` if you alias it."""

from __future__ import annotations

import os

PROG = os.environ.get("CURATOR_PROG", "curator")


def cmd(sub: str) -> str:
    return f"{PROG} {sub}".strip()
