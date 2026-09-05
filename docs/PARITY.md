# Parity map: Hermes Agent → herdr-curator

Upstream: `NousResearch/hermes-agent` @ `79445a49` (2026-09-04). Each row is a
module-level correspondence; bodies are ports (logic, messages, edge cases and
error strings preserved) unless listed under *Deviations*. Message hints read
`curator <verb>` where Hermes prints `hermes curator <verb>` (`CURATOR_PROG`).

| Hermes | Plugin | Notes |
|---|---|---|
| `hermes_constants.get_hermes_home` | `paths.get_home` | `CURATOR_HOME` > `HERMES_HOME` > `~/.hermes`; `~` via `Path.home()` |
| `hermes_cli.config.load_config / load_config_readonly / cfg_get` | `config` | JSON primary, YAML if PyYAML present; same signature cache |
| `hermes_cli/config_defaults.py` (`curator`, `skills`, `auxiliary.curator`) | `config.DEFAULT_CONFIG` | identical defaults; plus plugin-only `skills.dir`, `auxiliary.curator.command` |
| `utils.atomic_write_text / atomic_json_write` | `fsutil` | |
| `hermes_cli.lifecycle.has_hook / invoke_hook` | `lifecycle` | in-process registry (`register_hook`) |
| `hermes_cli.sizefmt.format_bytes` | `sizefmt.format_bytes` | |
| `agent/skill_utils.py` | `skill_utils` | `EXCLUDED_SKILL_DIRS`, `SKILL_SUPPORT_DIRS`, `is_excluded_skill_path`, `is_skill_support_path`, `parse_frontmatter`, `yaml_load`, `iter_skill_index_files` (org gating), `get_external_skills_dirs`, `get_skill_create_dir`, `get_all_skills_dirs`, `get_project_skills_dirs`, `is_external_skill_path`, `normalize_skill_lookup_name`, `skill_matches_platform`, `get_disabled_skill_names`, `extract_skill_description`, `is_skill_description_truncated_for_prompt`, `SKILL_PROMPT_DESC_LIMIT=60`, `ESSENTIAL_SKILLS` |
| `tools/skill_provenance.py` | `skill_provenance` | verbatim |
| `tools/skill_usage.py` | `skill_usage` | every public function; `_locked_update` flock; `PROTECTED_BUILTIN_SKILLS` (empty upstream too) |
| `tools/skill_ledger.py` | `skill_ledger` | every public function incl. `fill_snapshot_from_curator_backup`, `rollback_entry` |
| `agent/curator_backup.py` | `curator_backup` | every public function; cron link restore |
| `cron/jobs.py` (`referenced_skill_names`, `rewrite_skill_refs`, `_normalize_skill_list`, `_canonical_skill_ref`, `load_jobs`, `save_jobs`, `_jobs_lock`) | `cron_jobs` | store format `{"jobs": [...], "updated_at"}`; `create_job`/`get_job` minimal |
| `tools/fuzzy_match.py` | `fuzzy_match` | verbatim strategy chain |
| `tools/path_security.py` | `path_security` | |
| `tools/skill_manager_guards.py` | `skill_manager_guards` | all guards; org auto-propose returns `None` |
| `tools/skill_manager_batch.py` | `skill_manager_batch` | |
| `tools/skill_manager_tool.py` | `skill_manager` | validation, `_find_skill`, create/edit/patch/delete/write_file/remove_file, `_record_success`, `skill_manage`, `SKILL_MANAGE_SCHEMA` |
| `tools/skills_tool.py` (`skills_list`, `skill_view`, `_skill_view_with_bump`) | `skills_tool` (`skill_view_with_bump`) | collision refusal, disabled/platform gates, linked files, file serving, read marks, view+use bump |
| `agent/curator.py` | `curator` | state, gates, `should_run_now`, `apply_automatic_transitions`, prompts, `_classify_removed_skills`, `_parse_structured_summary`, `_extract_absorbed_into_declarations`, `_reconcile_classification`, `_build_rename_summary`, `_rewrite_cron_refs`, `_write_run_report`, `_render_report_markdown`, `_render_candidate_list`, `_consolidation_pass`, `run_curator_review`, `_resolve_review_runtime`, `_merge_request_overrides`, `maybe_run_curator` |
| `agent/curator._run_llm_review` / `_resolve_review_provider` | `llm_review.run_review` | see *Deviations* — same return shape |
| `toolsets` "skills" toolset served to the fork | `mcp_server` | `skills_list`, `skill_view`, `skill_manage` over MCP stdio |
| `hermes_cli/curator.py` (`hermes curator …`) | `cli` | all 17 subcommands, flags, messages, exit codes |
| `hermes_cli/update_cmd_maint.py` `_print_curator_first_run_notice`, `_print_curator_recent_run_notice`, `_format_time_ago` | `notices` | + list-returning variants for Herdr notifications |
| `cli.py` `_tui_startup_background_maintenance` (session start, idle=∞) | `herdr.startup` → `tick(idle=∞)` | |
| `gateway/run.py` `_housekeeping_curator` (60 s tick, idle=∞) | `herdr.daemon` | idle measured from `herdr agent list` instead of assumed ∞ |
| `/curator` slash command, dashboard `/api/curator/*` | Herdr actions/panes (`herdr.ACTIONS`, `herdr.PANES`) | different host, same operations |

## Deviations (deliberate)

1. **Agent runtime.** Hermes forks `AIAgent` in-process with the `skills` toolset. The plugin spawns a headless coding agent (`claude -p` / `opencode run` / custom argv) with built-in tools disabled and one MCP server. Contract preserved: three tools, `background_review` origin, no shell, ledgered mutations, `tool_calls` + `final` returned in the same `llm_meta` shape. `resolve_runtime_provider` (credential pools, api_mode, ACP commands) has no equivalent; `auxiliary.curator.{provider,model}` map to runner kind + model and `api_key`/`base_url`/`extra_body` are resolved but unused by the built-in runners.
2. **Dry-run enforcement.** `mcp-serve --dry-run` refuses mutating `skill_manage` actions (Hermes relies on the prompt banner). Reads and logging are unchanged.
3. **Config format.** `config.json` instead of `config.yaml` (no third-party deps); YAML read only if PyYAML is importable. Same key tree.
4. **YAML frontmatter.** Without PyYAML a built-in block-YAML subset parser is used (mappings, sequences, flow lists, quoted/plain scalars, comments; YAML-1.1 booleans like Hermes's PyYAML). Malformed input falls back to Hermes's `key: value` split.
5. **Not ported (Hermes-runtime features with no Herdr counterpart):** the security scanner (`skills.guard_agent_created`, default off), the staged write-approval gate (`skills.write_approval`), skill lint findings, skills-sync push/org proposals, plugin-provided skills, SKILL.md template variables / inline shell, readiness (config-var) checks, project-skill quarantine, the per-session repeat-view dedup stub, other-profile lookups in not-found errors, the `on_skill_lifecycle` plugin bus (kept as an in-process registry), `PLUGIN-COMPAT` re-export blocks.
6. **Idle measurement.** Hermes passes `idle_for_seconds=inf` from both trigger points (gating is effectively interval-only). The daemon here measures real idleness from Herdr agent states, so `min_idle_hours` is honoured; the startup tick still passes `inf` like the CLI.
7. **Program name in hints:** `curator …` instead of `hermes curator …`.
8. **Reports/notices surfaces:** the once-per-run rename map is shown at Herdr session start (Hermes: on `hermes update`); run summaries become Herdr notifications (Hermes: `💾` console line / gateway log).

## Behaviours verified identical (selected)

- first observation seeds `last_run_at` and defers a full interval
- never-used grace floor; cron-referenced protection incl. absolute-path refs; pinned bypass; first-sight seeding of built-ins
- archive flattening + `-YYYYMMDDHHMMSS` collision suffix; restore preferring exact name then newest timestamped, never a prefix sibling; hub/bundled shadow refusals; suppression list toggling
- ledger: actor derivation (`user` / `curator` / `agent`), blob dedupe, fail-closed rollback with safety entry, path containment, complete-package fill from the newest snapshot, traversal rejection
- snapshots: `-NN` same-second ids, `keep` pruning with rollback-target protection, `.git`/`.hub`/`.curator_backups` exclusion (nested too), staged extract with exact restoration on failure, cron link reconciliation by job id
- classification precedence: `absorbed_into` > structured YAML block (existing umbrella) > tool-call audit > no-evidence fallback; report sections and wording; rename summary cap of 10 + pin hint
- `skill_manage`: validation messages, categorized-path resolution, fuzzy patch with recovery guidance and `file_preview`, symlink escape refusals, pinned-delete refusal text, rmtree guards, batch atomicity/clobber rules, background-review ownership stability, read-before-write per exact file, fail-closed consolidation delete, recoverable archive on verified consolidation
- CLI: pin/unpin messaging for unmanaged vs managed vs ineligible, status layout, prune/adopt/purge confirmations and exit codes, usage sorting/filters
