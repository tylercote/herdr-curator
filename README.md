# herdr-curator

A [Herdr](https://herdr.dev) plugin that ports the **Curator** from
[Hermes Agent](https://github.com/NousResearch/hermes-agent) — its skill
auto-management system — with function-level parity:

- **usage telemetry** for every `SKILL.md` (`view` / `use` / `patch` counters in a `.usage.json` sidecar)
- **lifecycle** `active → stale (30d) → archived (90d)` for curator-managed skills, never deleting
- **pin / adopt / restore / archive / prune** with the same rules Hermes applies (bundled, hub-installed, external and protected skills are off-limits)
- **whole-tree tar.gz snapshots** before every real run + `rollback`
- **per-mutation audit ledger** (JSONL + content-addressed blobs) with single-entry `rollback <id>`
- **opt-in LLM consolidation** that forks a headless coding agent whose *only* tools are `skills_list` / `skill_view` / `skill_manage`, then classifies every removal as consolidated-into-umbrella or pruned and writes `run.json` + `REPORT.md`
- **cron job protection**: skills referenced by `cron/jobs.json` are never auto-archived, and references are rewritten after a consolidation

Stdlib Python ≥ 3.9. No dependencies. `ARCHITECTURE.md` explains how it is built; `docs/PARITY.md` maps every Hermes function to its port and lists the deliberate deviations.

## Install

```sh
herdr plugin install tylercote/herdr-curator          # from GitHub
# or, from a local checkout:
herdr plugin link /path/to/herdr-curator
```

Bind the actions in `~/.config/herdr/config.toml` (all optional — everything is also reachable from `curator` on the command line):

```toml
[[keys.command]]
key = "prefix+c"
type = "plugin_action"
command = "curator.console"
description = "curator console"

[[keys.command]]
key = "prefix+shift+c"
type = "plugin_action"
command = "curator.status"
description = "curator status"

# The rest, if you want single keys for them:
# command = "curator.run"          # prune-only pass now
# command = "curator.dry-run"      # preview, mutates nothing
# command = "curator.consolidate"  # prune + LLM umbrella-building pass
# command = "curator.report"       # last REPORT.md
```

Then `herdr config check && herdr server reload-config`. Ctrl-click any
`file://…/logs/curator/<stamp>/REPORT.md` path in a pane to open that report.

## Where the skills live

Defaults match Hermes: home `~/.hermes` (or `$HERMES_HOME`), skills in `<home>/skills`. Point it at any other `SKILL.md` tree — e.g. Claude Code's — with the plugin config:

```sh
$EDITOR "$(herdr plugin config-dir curator)/config.json"
```

```json
{
  "skills": { "dir": "~/.claude/skills" },
  "curator": { "consolidate": false, "prune_builtins": true },
  "auxiliary": { "curator": { "provider": "claude", "model": "claude-sonnet-5", "timeout": 600 } }
}
```

The key tree is Hermes's `config.yaml` verbatim (see `config.example.json`); if PyYAML happens to be importable, a real `~/.hermes/config.yaml` is read too. Environment overrides: `CURATOR_HOME`, `CURATOR_SKILLS_DIR`, `CURATOR_CONFIG`.

## How it runs

Exactly like Hermes, the curator is **inactivity-triggered, not a cron job**:

1. When a Herdr session starts, the plugin's startup hook shows the first-run notice, performs one fully-idle tick (Hermes: CLI session start) and spawns a detached 60-second ticker (Hermes: the gateway housekeeping tick).
2. A tick runs a pass only if `interval_hours` (7 d) have passed since the last run **and** no Herdr agent has been `working` for `min_idle_hours` (2 h).
3. The first observation only seeds the clock — a fresh install never mutates anything until a full interval later. Preview any time with `curator run --dry-run`.
4. A real pass: snapshot → deterministic stale/archive transitions → (if `curator.consolidate`) the LLM pass → `~/.hermes/logs/curator/<stamp>/REPORT.md` → a Herdr notification with the rename map.

## CLI

```sh
curator status            curator run [--dry-run] [--consolidate] [--background]
curator usage [--json]    curator pause | resume
curator pin <s>           curator unpin <s>
curator adopt <s>...      curator adopt --all-unmanaged [--dry-run] [--yes]
curator list-unmanaged    curator list-archived
curator archive <s>       curator restore <s>       curator prune [--days N]
curator backup            curator rollback [--list | --id <snap> | <ledger-entry-id>]
curator ledger [--skill s] [--limit n]
curator purge [--days N] [--dry-run]
```

(`curator` = `python3 "$(herdr plugin config-dir curator | sed 's#/config/[^/]*$##')/…"` — simplest is to alias `bin/curator` from your checkout, or run `herdr plugin action invoke curator.console`.)

## Telemetry from your agents

Hermes bumps counters when its own agent loads a skill. Other agents can report the same events:

```sh
curator bump use <skill>     # skill loaded into a conversation
curator bump view <skill>    # skill file viewed
curator bump patch <skill>   # skill edited
```

For Claude Code, a `PostToolUse` hook on the `Skill` tool that runs `curator bump use "$SKILL_NAME"` gives the curator the same signal Hermes gets natively. Any agent that speaks MCP can also mount `curator mcp-serve --foreground` and get the full ledgered `skill_manage` surface.

## The LLM consolidation pass

Off by default (`curator.consolidate: false`), like Hermes. When on, or with `curator run --consolidate`, the pass spawns `claude -p` (or `opencode run`, or any argv template via `auxiliary.curator.provider = "command"`) with **no built-in tools** and a single MCP server, `curator mcp-serve`, that exposes the skills toolset with the background-review origin bound: ownership, read-before-write and fail-closed-delete guards are enforced in the server, every mutation is ledgered as `actor=curator`, and the tool-call log drives the consolidated-vs-pruned classification in the report.

## Development

```sh
uv run --with pytest pytest          # or: python3 -m pytest
herdr plugin link "$PWD"
```

MIT — a port of Nous Research's MIT-licensed Hermes Agent curator; see `LICENSE`.
