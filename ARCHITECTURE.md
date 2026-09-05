# Architecture

This document explains how `herdr-curator` is built and *why* each piece exists.
It is written for someone who has to modify the plugin months from now. Every
module also carries a docstring naming the Hermes file it mirrors;
`docs/PARITY.md` is the function-level map.

## 1. What the Curator is

Hermes Agent's self-improvement loop writes skills (`<name>/SKILL.md` +
optional `references/ templates/ scripts/ assets/`). Left alone, that library
fills with narrow one-incident skills. The **Curator** is the maintenance
system around it:

| Concern | Mechanism |
|---|---|
| know what is used | `.usage.json` sidecar: `use_count`, `view_count`, `patch_count`, timestamps, `created_by`, `state`, `pinned` |
| retire what is not | `active → stale → archived` transitions from the newest real activity; archive = `mv` into `.archive/`, never delete |
| never touch what is not yours | only skills with `created_by: agent` (or bundled built-ins when `prune_builtins`) are managed; hub-installed, external-dir, protected and pinned skills are untouchable; cron-referenced skills are exempt from auto-transitions |
| make everything reversible | tar.gz snapshot of the tree before every real pass; a JSONL ledger with before/after blobs for every mutation; whole-tree and single-entry rollback, each taking a safety capture first and failing closed |
| shrink the library intelligently | an optional LLM pass whose tool surface is exactly `skills_list / skill_view / skill_manage`, audited afterwards |
| tell the user | `run.json` + `REPORT.md` per run, a rename map in the summary, `status` |

Parity target: `NousResearch/hermes-agent` at `79445a49` (2026-09-04).

## 2. Layout on disk

```
<home>                     CURATOR_HOME | HERMES_HOME | ~/.hermes
├── config.json            plugin config (same key tree as Hermes config.yaml)
├── logs/curator/<stamp>/  run.json, REPORT.md, [cron_rewrites.json]
│   └── .runs/<stamp>/     LLM pass artifacts: prompt.txt, mcp.json, stdout/stderr, tool_calls.jsonl
├── .curator_backups/blobs/<sha256>          ledger blob store (content-addressed)
├── cron/jobs.json         scheduled jobs — skill references are protected + rewritten
└── skills/                CURATOR_SKILLS_DIR | config skills.dir | <home>/skills
    ├── <name>/SKILL.md [references/ templates/ scripts/ assets/]
    ├── <category>/<name>/SKILL.md
    ├── .usage.json        telemetry + provenance sidecar  (+ .usage.json.lock)
    ├── .curator_state     scheduler state
    ├── .curator_ledger.jsonl
    ├── .curator_backups/<utc-iso>/{skills.tar.gz, manifest.json, cron-jobs.json}
    ├── .archive/<name>[-YYYYMMDDHHMMSS]/
    ├── .bundled_manifest  "name:hash" lines: bundled built-ins
    ├── .hub/lock.json     hub-installed skills
    └── .curator_suppressed  pruned built-ins a re-seeder must not restore
```

The split — sidecars and snapshots *inside* `skills/`, blobs and logs *under
home* — is Hermes's and is load-bearing: snapshots exclude `.curator_backups`,
`.hub`, `.git`; the ledger blob store must survive a tree rollback.

## 3. Module graph

```
                    ┌──────────── host layer ────────────┐
   herdr-plugin.toml│  herdr.py   (startup/daemon/action/pane)
   bin/curator ─────┤  __main__.py (verb router)         │
                    │  cli.py     (`curator <verb>`)      │
                    │  notices.py (first-run / rename map)│
                    └──────────────┬─────────────────────┘
                                   ▼
                    curator.py  ── orchestrator: gates, transitions, LLM pass, classification, reports
                       │  ▲                          │
                       │  └── llm_review.py ───► spawns claude/opencode/argv ──MCP──► mcp_server.py
                       │                                                             │
   ┌───────────────────┼─────────────────────────────────────────────────────────────┘
   ▼                   ▼
skill_usage.py     skill_manager.py ◄─ skill_manager_guards.py, skill_manager_batch.py, fuzzy_match.py
(sidecar, lifecycle, skills_tool.py    (skill_manage)              (ownership / read-before-write /
 archive/restore)  (skills_list,                                    consolidation delete / pinned / rmtree)
   │                skill_view)
   ▼
skill_ledger.py    curator_backup.py    cron_jobs.py
(JSONL + blobs,    (tar.gz snapshots,   (referenced_skill_names,
 rollback_entry)    rollback)            rewrite_skill_refs)
   └────────────── foundations: paths.py  config.py  fsutil.py  skill_utils.py  skill_provenance.py  lifecycle.py
```

Dependency direction is strictly downward; the foundations import nothing above them.
`paths` resolves everything at call time from the environment (no import-time
caching), which is why tests need no module reloads.

## 4. The lifecycle pass (`curator.apply_automatic_transitions`)

For each row of `skill_usage.curated_report()` (managed skills + pinned
eligible ones):

1. `pinned` or cron-referenced → skip.
2. no persisted record (`_persisted=False`) → `seed_record_if_missing`; the clock starts *now* (so enabling `prune_builtins` never mass-archives on the first pass).
3. `anchor = last_activity_at or created_at or now` where `last_activity_at = max(last_used, last_viewed, last_patched)` — **`created_at` is excluded** so never-active skills stay distinguishable.
4. `use_count == 0 and anchor > stale_cutoff` → grace floor: never archive a never-used skill younger than `stale_after_days`; un-stale it if it was stale.
5. `anchor ≤ archive_cutoff` → `archive_skill` with ledger actor `curator`; `≤ stale_cutoff` → `stale`; `> stale_cutoff and stale` → `active`.

`archive_skill` moves the directory flat into `.archive/` (timestamp suffix on
collision), toggles the suppression list for built-ins, sets `state=archived`,
and records a `complete_package` ledger entry — one whose before-manifest is
filled from the newest snapshot so a skill whose support files were re-homed
earlier in a consolidation still rolls back whole (Hermes issue #96962).

## 5. Provenance: who may be curated

`created_by` is stored per record but *consumed as a policy flag*: "may
autonomous curation touch this?". Only two writers set it to `agent`:

- `skill_manage(action=create)` running under the `background_review` write origin (the LLM pass), and
- `curator adopt` (a user declaration; the inactivity clock is *not* reset).

Foreground creates leave it `None`; pre-marker records have no key. Both are
**unmanaged**: curation-eligible but invisible to transitions, listed by
`list-unmanaged`, and refused by the fork's ownership guard until adopted.
Provenance is declared, never inferred from telemetry.

Eligibility (`is_curation_eligible`) is orthogonal: protected built-ins, hub
skills and external-dir skills are *never* eligible; bundled built-ins only
with `prune_builtins`. `set_state`, `set_pinned`, `mark_agent_created` and
`archive_skill` are gated on it, while `bump_*` telemetry is recorded for
**every** skill (observability without jurisdiction).

## 6. Safety rails around mutation

| Rail | Where | Behaviour |
|---|---|---|
| pre-run snapshot | `run_curator_review` | `snapshot_skills("pre-curator-run")`; best-effort, never aborts a pass |
| ledger | `skill_ledger.record_mutation` via `skill_manager._record_success`, `skill_usage._relocate`, `cli purge` | append-only JSONL, blobs deduped by sha256; **telemetry, never a gate** |
| single-entry rollback | `skill_ledger.rollback_entry` | validates every path is under home, pre-checks every blob, appends a `pre-rollback` safety entry, then restores before-files and removes files the mutation created; fails closed |
| whole-tree rollback | `curator_backup.rollback` | safety snapshot first (protected from its own prune), stage current tree, extract with `..`/absolute rejection, carry excluded subtrees (`.git`, `.hub`) back, restore cron `skills`/`skill` fields by job id |
| pinned guard | `skill_manager_guards._pinned_guard` | pin blocks **delete only**; essential skills always |
| rmtree guard | `_validate_delete_target` | never delete a symlink, a skills root, or a path outside every root |
| background-review guards | `_background_review_write_guard`, `_read_before_write_guard`, `_curator_consolidation_delete_guard` | see §7 |

## 7. The LLM consolidation pass

Hermes forks its in-process `AIAgent` with `enabled_toolsets=["skills"]`, no
terminal, `max_iterations=9999`, write origin `background_review`. Herdr has
no agent runtime, so `llm_review.run_review` reproduces the same *contract*:

```
run_review(prompt)
  ├─ resolve_runner(config)            auxiliary.curator.{provider,model} → claude | opencode | command | auto
  ├─ run_dir = logs/curator/.runs/<stamp>/      prompt.txt, mcp.json, stdout.txt, stderr.txt
  ├─ build_mcp_config()                one server: `python bin/curator mcp-serve --run-dir <run_dir> [--dry-run]`
  ├─ build_argv(spec, …)               claude: -p, --output-format json, --mcp-config, --strict-mcp-config,
  │                                            --tools "" (NO built-ins), --allowedTools mcp__curator__{skills_list,skill_view,skill_manage},
  │                                            --permission-mode dontAsk, --no-session-persistence
  │                                    opencode: run --format json --agent curator (+ OPENCODE_CONFIG with tools {"*": false, "curator_*": true})
  │                                    command: user argv with {prompt} {prompt_file} {mcp_config} {model}
  ├─ spawn (timeout = auxiliary.curator.timeout, default 600 s)
  └─ llm_meta = {final, summary[:240], model, provider, tool_calls ← run_dir/tool_calls.jsonl, error}
```

`mcp_server.Server` is the fork's whole world. On startup it binds
`skill_provenance.BACKGROUND_REVIEW`, so inside the server process:

- `skill_view` marks the exact file it served (`mark_background_review_skill_read`); a later `skill_manage` write to a file that was **not** viewed in this review is refused (`_read_before_write_required`). Marks live in a ContextVar holding a lock-protected set, so copied contexts within one review share them and separate reviews do not.
- ownership guard: pinned, external, protected, hub, bundled and **not-curator-managed** skills are refused with the `curator adopt <name>` hint — and the answer is stable across repeated identical attempts (the guard keys on the record's *value*, not its existence; Hermes #67140).
- delete requires `absorbed_into=<existing umbrella>`; `""`/omitted is refused with `_fail_closed` (Hermes #29912). A verified consolidation **archives** (recoverable) instead of `rmtree`.
- every call is appended to `tool_calls.jsonl` as `{name, arguments}`; ledger entries carry `actor=curator`.
- `--dry-run` refuses mutating actions at the server. *Hermes relies on the prompt banner alone; this is deliberate hardening and the only behavioural addition in the pass.*

After the pass, `curator._diff_and_classify` decides for every skill that
disappeared between the before/after `curated_report()`:

1. `absorbed_into` declared at delete (authoritative): target exists → consolidated; `""` → pruned.
2. the model's `## Structured summary` YAML block (`consolidations:` / `prunings:`), when the named umbrella exists.
3. the tool-call audit: a `skill_manage` call on a *different surviving or new* skill whose `file_path` (whole path component, `-`/`_` normalised) or content (word boundary) mentions the removed name.
4. otherwise pruned (`no-evidence fallback`).

Consolidations rewrite cron job references to the umbrella; prunes drop them
(`cron_jobs.rewrite_skill_refs`). The report records source and evidence per
entry, plus `⚠` when the model named an umbrella that does not exist.

## 8. Scheduling inside Herdr

Hermes calls `maybe_run_curator(idle_for_seconds=∞)` at CLI start and every
60 s from the gateway. Here:

- `[[startup]]` → `curator startup`: notices as Herdr notifications, one tick with idle = ∞, spawn `curator daemon` detached (`start_new_session`).
- `curator daemon`: pidfile in `HERDR_PLUGIN_STATE_DIR` (stale pids reclaimed), loop `tick(); sleep 60`, exits when `HERDR_SOCKET_PATH` disappears.
- `tick`: `idle_for_seconds()` = seconds since any `herdr agent list` entry was `working`/`blocked` (persisted in `activity.json` so the clock survives restarts); `None` when Herdr is unavailable, which `maybe_run_curator` treats as not-measurable → fully idle (CLI semantics).
- `should_run_now`: enabled, not paused, `last_run_at` present **and** older than `interval_hours`; a missing `last_run_at` is seeded and deferred (fresh installs never mutate on tick one).

Run summaries reach the user via `herdr notification show`, and the once-per-run rename map via the startup notice (Hermes shows it on `hermes update`).

## 9. Configuration

`config.py` deep-merges `DEFAULT_CONFIG` ← optional `<home>/config.yaml` (PyYAML only) ← `config.json` (`CURATOR_CONFIG` > `$HERDR_PLUGIN_CONFIG_DIR/config.json` > `<home>/config.json`), cached on `(mtime_ns, size)`. Keys and defaults are Hermes's:

```
curator.{enabled, interval_hours=168, min_idle_hours=2, stale_after_days=30, archive_after_days=90,
         consolidate=false, prune_builtins=true, archive_ttl_days=0, backup.{enabled=true, keep=5}}
skills.{dir (plugin-only), external_dirs, create_dir, project_discovery, trusted_project_dirs, disabled,
        platform_disabled, ledger=true}
auxiliary.curator.{provider=auto, model, timeout=600, command (plugin-only argv template)}
model.{provider, default}          ← fallback for "auto"
curator.auxiliary.{provider,model} ← legacy, still honoured with a deprecation log line
```

## 10. Testing strategy

Red/green from the Hermes suite: `tests/` ports the upstream tests for every
module (`test_curator*.py`, `test_skill_usage.py`, `test_skill_ledger.py`,
`test_curator_backup.py`, `test_cron_jobs.py`, `test_cli.py`,
`test_skill_manager.py`, …) so parity is *asserted*, then adds the plugin's own
surfaces (`test_llm_review.py` with an injected spawner, `test_mcp_server.py`
including a real `bin/curator mcp-serve` subprocess round-trip, `test_herdr.py`
with a fake `herdr` binary and manifest/README consistency checks). The suite
is offline; nothing spawns a real model. Run `uv run --with pytest pytest`.

## 11. Extending

- New Herdr action: add to `ACTIONS` in `herdr.py`, a `[[actions]]` block in the manifest, and the keybinding line in the README (a test enforces both).
- New runner: add a branch to `llm_review.build_argv` / `parse_final`; keep the invariant that the agent has **no** tools besides the MCP server.
- New telemetry source: call `curator bump {view,use,patch} <skill>` or `curator.skill_usage.bump_*` — the record shape is fixed by Hermes and read by `latest_activity_at`.
- Anything that mutates a skill directory must go through `skill_manage` or `skill_usage.archive_skill/restore_skill` so it is ledgered; a shell `mv` under `skills/` is exactly the bug class the ledger exists to catch.
