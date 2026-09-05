"""``python -m curator`` / ``bin/curator`` — one entrypoint for every surface.

    curator <hermes verb> ...      the ported `hermes curator` CLI (status, run, pin, ...)
    curator mcp-serve ...          skills toolset over MCP for the consolidation fork
    curator startup                Herdr [[startup]] hook: notices, one tick, spawn daemon
    curator daemon [--interval S]  the 60 s scheduler tick loop (single instance)
    curator tick [--idle S|inf]    one scheduler observation
    curator action <id>            Herdr [[actions]] dispatch
    curator pane <id>              Herdr [[panes]] entrypoints
    curator bump view|use|patch N  telemetry hooks for other agents (see README)
"""

from __future__ import annotations

import sys
from typing import List, Optional

_EXTRA = ("mcp-serve", "startup", "daemon", "tick", "action", "pane", "bump")


def _usage() -> str:
    from curator.cli import _SUBCOMMANDS
    verbs = ", ".join(n for n, *_ in _SUBCOMMANDS)
    return (f"usage: curator <verb> ...\n\n  Hermes curator verbs: {verbs}\n"
            f"  Plugin verbs: {', '.join(_EXTRA)}\n\nRun `curator <verb> --help` for details.")


def main(argv: Optional[List[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] in ("-h", "--help") or not argv:
        print(_usage())
        return 0
    verb, rest = argv[0], argv[1:]
    if verb == "mcp-serve":
        from curator.mcp_server import main as mcp_main
        return mcp_main(rest)
    if verb in ("startup", "daemon", "tick", "action", "pane", "bump"):
        return _plugin_verb(verb, rest)
    from curator.cli import cli_main
    return cli_main(argv)


def _plugin_verb(verb: str, rest: List[str]) -> int:
    import argparse
    from curator import herdr
    if verb == "startup":
        return herdr.startup()
    if verb == "daemon":
        p = argparse.ArgumentParser(prog="curator daemon")
        p.add_argument("--interval", type=float, default=60.0)
        return herdr.daemon(interval=p.parse_args(rest).interval)
    if verb == "tick":
        p = argparse.ArgumentParser(prog="curator tick")
        p.add_argument("--idle", default=None, help="idle seconds, 'inf', or omit to measure via Herdr")
        idle = p.parse_args(rest).idle
        kwargs = {} if idle is None else {"idle_for_seconds": float(idle)}
        result = herdr.tick(**kwargs)
        print("curator: run started" if result else "curator: nothing to do")
        return 0
    if verb == "action":
        return herdr.action(rest[0] if rest else "")
    if verb == "pane":
        return herdr.pane(rest[0] if rest else "")
    if verb == "bump":
        from curator import skill_usage
        p = argparse.ArgumentParser(prog="curator bump", description="Record skill telemetry (view | use | patch).")
        p.add_argument("kind", choices=("view", "use", "patch"))
        p.add_argument("skill")
        a = p.parse_args(rest)
        {"view": skill_usage.bump_view, "use": skill_usage.bump_use, "patch": skill_usage.bump_patch}[a.kind](a.skill)
        return 0
    return 2


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
