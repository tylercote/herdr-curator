// herdr-curator: forwards skill loads and edits to the curator's usage telemetry.
// Managed by `curator hooks install pi`; edits here are overwritten on reinstall.
// pi loads a skill by `read`ing its SKILL.md, so a read inside a skill dir is a load.
import type { ExtensionAPI } from "@mariozechner/pi-coding-agent"
import { execFile } from "node:child_process"

const CURATOR_HOOK = "__CURATOR_HOOK__"
const TOOLS = new Set(["read", "edit", "write"])

function forward(payload: unknown): void {
  try {
    const child = execFile(CURATOR_HOOK, ["pi"], { timeout: 5000 }, () => {})
    child.on("error", () => {})
    child.stdin?.on("error", () => {})
    child.stdin?.end(JSON.stringify(payload))
  } catch {
    // telemetry must never break the host
  }
}

export default function (pi: ExtensionAPI) {
  const pending = new Map<string, unknown>()
  pi.on("tool_execution_start", async (event) => {
    if (TOOLS.has(event.toolName)) pending.set(event.toolCallId, event.args)
  })
  pi.on("tool_execution_end", async (event) => {
    const args = pending.get(event.toolCallId)
    pending.delete(event.toolCallId)
    if (!event.isError && TOOLS.has(event.toolName) && args) forward({ tool: event.toolName, args })
  })
}
