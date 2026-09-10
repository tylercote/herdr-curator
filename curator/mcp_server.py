"""The ``skills`` toolset served over MCP (stdio, newline-delimited JSON-RPC 2.0).

This is the fork's ENTIRE tool surface: ``skills_list``, ``skill_view``,
``skill_manage``. The process unconditionally binds the ``background_review``
write origin for its whole life — there is no way to serve without it — so the
ownership / read-before-write / consolidation-delete guards fire for every
caller, whether the curator's own consolidation fork or an agent the user
mounted the server in: non-managed (user, external, pinned) skills can never
be edited through this server. Every ledger entry is tagged ``actor=curator``;
``skill_view`` bumps the view counter only — an agent's reading is not use.

Every ``tools/call`` is appended to ``<run-dir>/tool_calls.jsonl`` as
``{"name", "arguments"}`` (arguments as the JSON string the agent sent), which
is what ``curator`` audits to classify removals as consolidated vs pruned.

``--dry-run`` additionally refuses mutating ``skill_manage`` actions at the
server rather than trusting the prompt banner alone.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import Any, Dict, Optional

from curator.skill_manager import SKILL_MANAGE_SCHEMA
from curator.skills_tool import SKILL_VIEW_SCHEMA, SKILLS_LIST_SCHEMA

logger = logging.getLogger(__name__)

PROTOCOL_VERSION = "2025-06-18"
_MUTATING_ACTIONS = {"create", "edit", "patch", "delete", "write_file", "remove_file"}


def _tool_entry(schema: Dict[str, Any]) -> Dict[str, Any]:
    return {"name": schema["name"], "description": schema["description"], "inputSchema": schema["parameters"]}


def _flat_manage_schema() -> Dict[str, Any]:
    """The advertised schema is the operations array; the flat single-op shape is accepted too."""
    schema = json.loads(json.dumps(SKILL_MANAGE_SCHEMA))
    props = schema["parameters"]["properties"]
    props.update({k: v for k, v in schema["parameters"]["properties"]["operations"]["items"]["properties"].items()})
    schema["parameters"]["required"] = []
    return schema


TOOLS = [_tool_entry(SKILLS_LIST_SCHEMA), _tool_entry(SKILL_VIEW_SCHEMA), _tool_entry(_flat_manage_schema())]


class Server:
    def __init__(self, *, tool_log: Optional[Path] = None, dry_run: bool = False) -> None:
        from curator.skill_provenance import BACKGROUND_REVIEW, set_current_write_origin
        self.tool_log = Path(tool_log) if tool_log else None
        self.dry_run = dry_run
        self._origin_token = set_current_write_origin(BACKGROUND_REVIEW)  # always: the guards are not optional

    # --- JSON-RPC -------------------------------------------------------------

    def handle(self, request: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        method = request.get("method")
        rid = request.get("id")
        params = request.get("params") or {}
        if method == "initialize":
            return self._ok(rid, {"protocolVersion": params.get("protocolVersion") or PROTOCOL_VERSION,
                                  "capabilities": {"tools": {}},
                                  "serverInfo": {"name": "curator", "version": _version()}})
        if method in ("notifications/initialized", "notifications/cancelled") or (method or "").startswith("notifications/"):
            return None
        if method == "ping":
            return self._ok(rid, {})
        if method == "tools/list":
            return self._ok(rid, {"tools": TOOLS})
        if method == "tools/call":
            name = params.get("name") or ""
            arguments = params.get("arguments") or {}
            text, is_error = self.call_tool(name, arguments if isinstance(arguments, dict) else {})
            return self._ok(rid, {"content": [{"type": "text", "text": text}], "isError": is_error})
        if rid is None:
            return None
        return {"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": f"Method not found: {method}"}}

    @staticmethod
    def _ok(rid, result) -> Dict[str, Any]:
        return {"jsonrpc": "2.0", "id": rid, "result": result}

    # --- tools --------------------------------------------------------------------

    def _log(self, name: str, arguments: Dict[str, Any]) -> None:
        if self.tool_log is None:
            return
        try:
            self.tool_log.parent.mkdir(parents=True, exist_ok=True)
            with open(self.tool_log, "a", encoding="utf-8") as fh:
                fh.write(json.dumps({"name": name, "arguments": json.dumps(arguments, ensure_ascii=False)}, ensure_ascii=False) + "\n")
        except Exception:
            logger.debug("tool log write failed", exc_info=True)

    def call_tool(self, name: str, arguments: Dict[str, Any]):
        """Returns ``(result_json_text, is_error)``."""
        if name not in ("skills_list", "skill_view", "skill_manage"):
            return json.dumps({"success": False, "error": f"Unknown tool '{name}'"}), True
        self._log(name, arguments)
        try:
            if name == "skills_list":
                from curator.skills_tool import skills_list
                text = skills_list(category=arguments.get("category"))
            elif name == "skill_view":
                from curator.skills_tool import skill_view_with_bump
                text = skill_view_with_bump(arguments)
            else:
                text = self._skill_manage(arguments)
        except Exception as e:  # noqa: BLE001 — a tool exception must surface as a tool error, not kill the server
            logger.debug("tool %s failed", name, exc_info=True)
            text = json.dumps({"success": False, "error": f"{name} failed: {e}"})
        try:
            is_error = not bool(json.loads(text).get("success"))
        except Exception:
            is_error = False
        return text, is_error

    def _skill_manage(self, arguments: Dict[str, Any]) -> str:
        from curator.skill_manager import _skill_manage_from
        if self.dry_run and self._mutates(arguments):
            return json.dumps({"success": False, "_dry_run": True,
                               "error": "DRY-RUN pass: skill_manage mutations are refused by the curator server. "
                                        "Describe the action you WOULD take in your summary instead."})
        return _skill_manage_from({k: v for k, v in arguments.items()})

    @staticmethod
    def _mutates(arguments: Dict[str, Any]) -> bool:
        ops = arguments.get("operations")
        if isinstance(ops, list):
            return any(isinstance(op, dict) and op.get("action") in _MUTATING_ACTIONS for op in ops) or not ops
        return arguments.get("action") in _MUTATING_ACTIONS


def _version() -> str:
    try:
        from curator import __version__
        return __version__
    except Exception:
        return "0"


def serve(server: Server, *, stdin=None, stdout=None) -> None:
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout
    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
        except Exception:
            response = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}}
        else:
            if isinstance(request, list):
                responses = [r for r in (server.handle(req) for req in request if isinstance(req, dict)) if r is not None]
                response = responses or None
            else:
                response = server.handle(request) if isinstance(request, dict) else None
        if response is not None:
            stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
            stdout.flush()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="curator mcp-serve", description="Serve the skills toolset over MCP (stdio).")
    parser.add_argument("--run-dir", default=None, help="Directory for tool_calls.jsonl (default: no log)")
    parser.add_argument("--dry-run", action="store_true", help="Refuse mutating skill_manage actions")
    args = parser.parse_args(argv)
    tool_log = Path(args.run_dir) / "tool_calls.jsonl" if args.run_dir else None
    logging.basicConfig(level=logging.WARNING, stream=sys.stderr)
    server = Server(tool_log=tool_log, dry_run=args.dry_run)
    serve(server)
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
