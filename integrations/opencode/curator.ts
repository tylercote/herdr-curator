// herdr-curator: forwards skill loads and edits to the curator's usage telemetry.
// Managed by `curator hooks install opencode`; edits here are overwritten on reinstall.
import type { Plugin } from "@opencode-ai/plugin"
import { execFile } from "node:child_process"

const CURATOR_HOOK = "__CURATOR_HOOK__"
const TOOLS = new Set(["skill", "read", "edit", "write", "multiedit"])

function forward(payload: unknown): void {
  try {
    const child = execFile(CURATOR_HOOK, ["opencode"], { timeout: 5000 }, () => {})
    child.on("error", () => {})
    child.stdin?.on("error", () => {})
    child.stdin?.end(JSON.stringify(payload))
  } catch {
    // telemetry must never break the host
  }
}

export const CuratorPlugin: Plugin = async () => {
  const pending = new Map<string, unknown>()
  return {
    "tool.execute.before": async (input, output) => {
      if (TOOLS.has(input.tool)) pending.set(input.callID, output.args)
    },
    "tool.execute.after": async (input) => {
      const args = (input as { args?: unknown }).args ?? pending.get(input.callID)
      pending.delete(input.callID)
      if (TOOLS.has(input.tool) && args) forward({ tool: input.tool, args, session: input.sessionID })
    },
  }
}
