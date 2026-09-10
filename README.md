# herdr-curator

A [Herdr](https://herdr.dev) plugin that keeps a coding agent's skill library
healthy. Agents write `SKILL.md` files for every incident they learn from;
left alone, the library fills with narrow one-off skills. The curator watches
what is actually used, retires what is not, and can merge overlapping skills
into umbrellas — every step reversible.

- **usage telemetry** for every `SKILL.md` (`view` / `use` / `patch` counters in a `.usage.json` sidecar)
- **lifecycle** `active → stale (30d) → archived (90d)` for curator-managed skills, never deleting
- **pin / adopt / restore / archive / prune** — bundled, hub-installed, external and protected skills are off-limits
- **whole-tree tar.gz snapshots** before every real run + `rollback`
- **per-mutation audit ledger** (JSONL + content-addressed blobs) with single-entry `rollback <id>`
- **opt-in LLM consolidation** that forks a headless coding agent whose *only* tools are `skills_list` / `skill_view` / `skill_manage`, then classifies every removal as consolidated-into-umbrella or pruned and writes `run.json` + `REPORT.md`
- **cron job protection**: skills referenced by `cron/jobs.json` are never auto-archived, and references are rewritten after a consolidation

Stdlib Python ≥ 3.9. No dependencies. `ARCHITECTURE.md` explains how it is built.

## Install

```sh
herdr plugin install tylercote/herdr-curator          # from GitHub
# or, from a local checkout:
herdr plugin link /path/to/herdr-curator
```

Bind the actions in `~/.config/herdr/config.toml` (all optional — everything is also reachable from `curator` on the command line). These use `shift` because Herdr already binds the unshifted `prefix+s` (settings) and `prefix+c` (new tab):

```toml
[[keys.command]]
key = "prefix+shift+s"
type = "plugin_action"
command = "curator.skills"
description = "skills: enable/disable, browse, edit"

[[keys.command]]
key = "prefix+shift+c"
type = "plugin_action"
command = "curator.console"
description = "curator console"

[[keys.command]]
key = "prefix+shift+i"
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

## Where things live

By default the curator curates **Claude Code's skills, `~/.claude/skills`**, and keeps
its own data where Herdr keeps plugin data:

| | path |
|---|---|
| skills | `CURATOR_SKILLS_DIR` → config `skills.dir` → `~/.claude/skills` |
| config | `CURATOR_CONFIG` → `$(herdr plugin config-dir curator)/config.json` (`~/.config/herdr/plugins/config/curator/`) |
| reports, ledger blobs, cron jobs, daemon state | `CURATOR_HOME` → Herdr's plugin state dir (`~/.local/state/herdr/plugins/curator/`) |

Herdr sets `HERDR_PLUGIN_CONFIG_DIR` / `HERDR_PLUGIN_STATE_DIR` for commands it spawns;
the fallbacks reconstruct the same directories (honouring `XDG_CONFIG_HOME` /
`XDG_STATE_HOME`), so `curator` typed in any shell and the scheduled plugin run agree
on every path. Setting `CURATOR_HOME` explicitly makes that directory a self-contained
tree instead (`<home>/skills`, `<home>/config.json`).

Hand-written skills are safe: curation is opt-in per skill, not per directory. A skill
is only touched once `created_by: agent` is on its `.usage.json` record — the curator
writes that for skills it creates, and `curator adopt <name>` is the only way an
existing one crosses over. Everything else shows up under `curator status` as
unmanaged and is never staled or archived. To point it at another tree:

```sh
$EDITOR "$(herdr plugin config-dir curator)/config.json"
```

```json
{
  "skills": { "dir": "~/work/skills" },
  "curator": { "consolidate": false, "prune_builtins": true },
  "auxiliary": { "curator": { "provider": "claude", "model": "claude-sonnet-5", "timeout": 600 } }
}
```

See `config.example.json` for the full key tree.

## How it runs

The curator is **inactivity-triggered, not a cron job**:

1. When a Herdr session starts, the plugin's startup hook shows the first-run notice, performs one fully-idle tick and spawns a detached 60-second ticker.
2. A tick runs a pass only if `interval_hours` (7 d) have passed since the last run **and** no Herdr agent has been `working` for `min_idle_hours` (2 h).
3. The first observation only seeds the clock — a fresh install never mutates anything until a full interval later. Preview any time with `curator run --dry-run`.
4. A real pass: snapshot → deterministic stale/archive transitions → (if `curator.consolidate`) the LLM pass → `<state>/logs/curator/<stamp>/REPORT.md` → a Herdr notification with the rename map.

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

(`curator` = `bin/curator` in your checkout — alias it, or run `herdr plugin action invoke curator.console`.)

## Skills TUI

`curator skills` (Herdr: `curator.skills`, `prefix+shift+s` above) is a checklist — `[✓]` = enabled, persisted to `skills.disabled` (or `skills.platform_disabled.<platform>` with `--platform`), essential skills never disableable, `c` toggles a whole category — with the curator's view of each skill: managed/pinned flags, lifecycle state, use/view/patch counts. Keys: `space` toggle · `Enter` view · `e` edit in `$EDITOR` (the change is ledgered as `actor=user` and counted as a patch, so it is rollback-able) · `p` pin · `a` adopt · `x` archive · `r` restore · `/` filter · `?` help. Scriptable: `curator skills --list [--json]`, `curator skills enable|disable <names>`.

## Telemetry from your agents

Any agent can report skill events:

```sh
curator bump use <skill>     # skill loaded into a conversation
curator bump view <skill>    # skill file viewed
curator bump patch <skill>   # skill edited
```

For Claude Code, a `PostToolUse` hook on the `Skill` tool that runs `curator bump use "$SKILL_NAME"` gives the curator its usage signal. Any agent that speaks MCP can also mount `curator mcp-serve --foreground` and get the full ledgered `skill_manage` surface.

## The LLM consolidation pass

Off by default (`curator.consolidate: false`). When on, or with `curator run --consolidate`, the pass spawns `claude -p` (or `opencode run`, or any argv template via `auxiliary.curator.provider = "command"`) with **no built-in tools** and a single MCP server, `curator mcp-serve`, that exposes the skills toolset with the background-review origin bound: ownership, read-before-write and fail-closed-delete guards are enforced in the server, every mutation is ledgered as `actor=curator`, and the tool-call log drives the consolidated-vs-pruned classification in the report.

## Development

```sh
uv run --with pytest pytest          # or: python3 -m pytest
herdr plugin link "$PWD"
```

MIT — see `LICENSE`.
