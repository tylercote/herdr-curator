"""``python -m curator`` / ``bin/curator`` — one entrypoint for every surface.

    curator <verb> ...             the curator CLI (status, run, pin, ...)
    curator skills [...]           the skills TUI: enable/disable, browse, edit
    curator setup [--yes]          interactive first-run flow: skills tree, hooks, pass runner + model, schedule
    curator mcp-serve ...          skills toolset over MCP for the consolidation fork
    curator startup                Herdr [[startup]] hook: notices, hook reconcile, spawn daemon
    curator daemon [--interval S] [--first-idle S]  the 60 s scheduler tick loop (single instance); runs every pass in-process
    curator tick [--idle S|inf]    one scheduler observation
    curator action <id>            Herdr [[actions]] dispatch
    curator pane <id>              Herdr [[panes]] entrypoints
    curator bump view|use|patch N  record telemetry for a skill by hand
    curator hooks install|uninstall|status|sync|launcher [host..]   telemetry hooks in claude / codex / opencode / pi
    curator hook <host>            receiver the host shims pipe tool calls into (stdin JSON)
"""

from __future__ import annotations

import sys
from typing import List, Optional

_EXTRA = ("skills", "setup", "mcp-serve", "startup", "daemon", "tick", "action", "pane", "bump", "hooks", "hook")


def _usage() -> str:
    from curator.cli import _SUBCOMMANDS
    verbs = ", ".join(n for n, *_ in _SUBCOMMANDS)
    return (f"usage: curator <verb> ...\n\n  Curator verbs: {verbs}\n"
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
    if verb == "skills":
        from curator.skills_tui import cli_main as skills_main
        return skills_main(rest)
    if verb == "setup":
        from curator.setup_wizard import setup_main
        return setup_main(rest)
    if verb == "hooks":
        from curator.integrations import hooks_main
        return hooks_main(rest)
    if verb == "hook":
        from curator.integrations import HOSTS, hook_main
        return hook_main(rest[0] if rest and rest[0] in HOSTS else "unknown")
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
        p.add_argument("--first-idle", type=float, default=float("inf"),
                       help="idle seconds assumed for the first tick ('inf' = fully idle); later ticks measure via Herdr")
        a = p.parse_args(rest)
        return herdr.daemon(interval=a.interval, first_idle=a.first_idle)
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
