# herdr-curator

A [Herdr](https://herdr.dev) plugin that keeps a coding agent's skill library
healthy. Agents write `SKILL.md` files for every incident they learn from;
left alone, the library fills with narrow one-off skills. The curator watches
what is actually used, retires what is not, and can merge overlapping skills
into umbrellas — every step reversible.

- **usage telemetry** for every `SKILL.md` (`view` / `use` / `patch` counters in a `.usage.json` sidecar)
- **lifecycle** `active → stale (30d) → archived (90d)` for curator-managed skills, never deleting
- **pin / adopt / restore / archive / prune** — three ownership classes and only one is ever curated: **managed** (the curator created it, or you ran `curator adopt`), **user** (everything else in the tree — never touched), **external** (registered read-only dirs — telemetry only)
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
# command = "curator.setup"        # (re)install telemetry hooks in your agents now
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
  "curator": { "consolidate": false },
  "auxiliary": { "curator": { "provider": "claude", "model": "claude-sonnet-5", "timeout": 600 } }
}
```

See `config.example.json` for the full key tree.

## How it runs

The curator is **inactivity-triggered, not a cron job**:

1. When a Herdr session starts, the plugin's startup hook shows the first-run notice and spawns a detached 60-second ticker whose first tick treats the session as fully idle. Every pass runs inside that ticker, so it cannot be cut short by the startup hook exiting.
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

The curator only learns anything if the agent that loads skills tells it. **That wiring is
automatic**: every Herdr session start reconciles the hooks — for each harness it finds on the
machine it installs (or refreshes) a hook, and it registers the harness's own skill directory so
loads of skills living there are counted too. A Herdr notification tells you when it changed
something. `Curator: setup telemetry hooks` (`curator.setup`) forces the same thing on demand.

| host | what gets installed | what it reports |
|---|---|---|
| **Claude Code** | a `PostToolUse` hook (`Skill\|Read\|Edit\|Write`) in `~/.claude/settings.json`, merged next to your existing hooks | `Skill` invocations and reads/edits of files inside a skill dir |
| **Codex** | `PostToolUse` (`Bash\|Edit\|Write\|Read\|apply_patch`) + `UserPromptSubmit` entries in `~/.codex/hooks.json` | `$skill` mentions in your prompt; shell reads of files inside a skill dir (Codex has no read tool — it `cat`s SKILL.md); `apply_patch` edits and shell redirects into a skill dir. **Codex won't run a new hook until you trust it: open Codex and run `/hooks`** (or pass `--dangerously-bypass-hook-trust` to `codex exec` for automation). |
| **OpenCode** | `~/.config/opencode/plugins/curator.ts` (`tool.execute.before/after`) | the `skill` tool, plus `read`/`edit`/`write` inside a skill dir |
| **pi** | `~/.pi/agent/extensions/curator.ts` (`tool_execution_start/end`) | `read`/`edit`/`write` inside a skill dir — pi loads a skill by reading its `SKILL.md` |

Every host forwards the raw tool call (or prompt) to `curator hook <host>`, which resolves the
path or name against the skills the curator actually scans and ignores everything else — so a
hook can never invent records. Loading a skill (the skill tool, or any read inside its directory)
counts as **view + use**: loading a skill to act on it *is* use, and the stale timer keys off
`last_used_at`. An edit or write inside a skill directory counts as a **patch**. The receiver
always exits 0 and logs each recorded event (and its source tool) to `<state>/hooks.log`; touch
`<state>/hooks.debug` to also log a summary of every payload a host sends.

**Nothing points at the checkout.** Host configs reference one stable launcher,
`<state>/bin/curator-hook`, which execs whatever plugin root Herdr last installed and exits 0
silently if the plugin is gone. So `herdr plugin update` never strands a hook, Codex never asks
you to re-trust an unchanged command, and uninstalling the plugin never spams your harness with
errors. Still, run `curator hooks uninstall` before `herdr plugin uninstall` to leave the host
configs clean.

**Skill directories.** pi and Codex read `~/.agents/skills`; OpenCode and Codex have their own
too. Reconcile adds the ones that exist to `skills.external_dirs` — telemetry only; external
skills are never staled, archived or consolidated. A skill you have *linked into* the curated
tree (`~/.claude/skills/x -> ~/.agents/skills/x`) stays local and manageable: the link is the
adoption.

Config (plugin `config.json`):

```json
{ "hooks": { "auto": true, "hosts": [], "register_skill_dirs": true } }
```

`auto: false` stops startup from installing anything (it still refreshes the launcher);
`hosts` restricts auto-install to a subset. The scriptable layer underneath:

```sh
curator hooks status           # per host, plus launcher + log location
curator hooks install [host]   # any of claude codex opencode pi; default: all detected
curator hooks uninstall [host] # no host = all, and removes the launcher
curator hooks sync             # exactly what session start does
```

Anything else can report directly with `curator bump use|view|patch <skill>`, and any agent
that speaks MCP can mount `curator mcp-serve` for the ledgered, rollback-able `skill_manage`
surface. The ownership guard is always on: through this server an agent can create skills (they
become managed) and edit managed ones, and is refused on yours, external and pinned skills.

## The LLM consolidation pass

Off by default (`curator.consolidate: false`). When on, or with `curator run --consolidate`, the pass spawns `claude -p` (or `opencode run`, or any argv template via `auxiliary.curator.provider = "command"`) with **no built-in tools** and a single MCP server, `curator mcp-serve`, that exposes the skills toolset with the background-review origin bound: ownership, read-before-write and fail-closed-delete guards are enforced in the server, every mutation is ledgered as `actor=curator`, and the tool-call log drives the consolidated-vs-pruned classification in the report.

## Safety

- **Only managed skills are ever modified autonomously.** A skill is managed only when its `.usage.json` record says `created_by: agent`, and exactly two things write that: an agent creating a *new* skill through `curator mcp-serve` (the LLM pass, or an agent you mounted the server in), and you running `curator adopt`. Hooks, ticks and startup never adopt anything.
- **It sees everything, touches only its own.** `skills_list` shows the model every skill — yours, external ones, its own — each labelled with its `owner`. That visibility exists so it never duplicates you: if a managed skill (or an umbrella it is about to build) is already covered by a user or external skill, the required move is to archive the managed one *absorbed into* the existing skill, which is recorded as a consolidation and modifies nothing outside the curator's own skills.
- **The LLM pass is fenced at the write layer.** Every `skill_manage` write goes through an ownership guard that refuses pinned, external and non-managed skills regardless of what the model asks; `skills_list` labels every row with its `owner` so the model is told what it may touch; and the pass's own reads count as *views*, not *uses*, so it can never keep a skill artificially alive.
- **Rollback comes in two sizes.** `curator rollback <ledger-entry-id>` undoes one curator mutation, file by file. `curator rollback --id <snapshot>` restores the **whole tree** — every skill, including ones the curator never managed, back to the moment of that snapshot (a safety snapshot is taken first, so it is itself undoable). Prefer the ledger form; reach for the snapshot form only when you want the entire tree back.
- **There is no unguarded server.** `curator mcp-serve` binds the ownership guard unconditionally; it has no flag or parameter to serve `skill_manage` without it. Mount it in your own agent and you get the same ledgered, rollback-able edit surface the curator uses, fenced to managed skills exactly the same way.

## Development

```sh
uv run --with pytest pytest          # or: python3 -m pytest
herdr plugin link "$PWD"
```

MIT — see `LICENSE`.
