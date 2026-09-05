"""herdr-curator — a Herdr plugin that ports Hermes Agent's skill "Curator" 1:1.

Package map (each module's docstring names the Hermes file it mirrors):

    paths                 hermes_constants.get_hermes_home + every derived path
    config                hermes_cli.config (load_config / cfg_get) + config_defaults subset
    fsutil                utils.atomic_write_text / atomic_json_write
    lifecycle             hermes_cli.lifecycle (has_hook / invoke_hook)
    skill_utils           agent/skill_utils.py (scanner, frontmatter, dirs, platform)
    skill_provenance      tools/skill_provenance.py (write-origin ContextVar)
    skill_usage           tools/skill_usage.py (.usage.json sidecar, lifecycle, archive)
    skill_ledger          tools/skill_ledger.py (JSONL audit ledger + blob rollback)
    curator_backup        agent/curator_backup.py (tar.gz snapshots + tree rollback)
    cron_jobs             cron/jobs.py subset (skill refs protection + rewrite)
    fuzzy_match           tools/fuzzy_match.py (patch engine)
    path_security         tools/path_security.py
    skill_manager_guards  tools/skill_manager_guards.py
    skill_manager_batch   tools/skill_manager_batch.py
    skill_manager         tools/skill_manager_tool.py (skill_manage)
    skills_tool           tools/skills_tool.py (skills_list / skill_view)
    curator               agent/curator.py (orchestrator, transitions, reports)
    cli                   hermes_cli/curator.py (`curator <subcommand>`)
    notices               hermes_cli/update_cmd_maint.py curator notices
    llm_review            agent/curator._run_llm_review — re-homed onto headless coding agents
    mcp_server            the "skills" toolset served over MCP to the review fork
    herdr                 Herdr host integration (context, notifications, panes, daemon)

See ARCHITECTURE.md for the narrative and docs/PARITY.md for the function-level map.
"""

__version__ = "0.1.0"
