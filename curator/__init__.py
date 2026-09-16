"""herdr-curator — a Herdr plugin that keeps an agent's skill library healthy.

Package map:

    paths                 state dir / skills dir / per-tree layout (resolved at call time)
    config                layered config.json + defaults (load_config / cfg_get)
    fsutil                atomic_write_text / atomic_json_write
    lifecycle             in-process skill lifecycle hook registry
    skill_utils           scanner, frontmatter, dirs, platform gating
    skill_provenance      write-origin ContextVar (foreground / background_review)
    skill_usage           usage.json sidecar: telemetry, state, archive/restore
    skill_ledger          JSONL audit ledger + blob store + single-entry rollback
    curator_backup        whole-tree tar.gz snapshots + rollback
    skills_tool           skills_list / skill_view
    skill_manager*        skill_manage (create / patch / delete) + guards + batch
    fuzzy_match           patch matching with recovery guidance
    mcp_server            the three skills tools over MCP for the consolidation fork
    curator               orchestrator: gates, transitions, LLM pass, classification, reports
    cli                   `curator <subcommand>`
    notices               first-run / recent-run notices
    llm_review            the consolidation pass on headless coding agents
    skills_tui            `curator skills`: enable/disable, browse, edit
    herdr                 Herdr host layer: startup, daemon, actions, panes
    integrations          telemetry hooks for host agents (claude / codex / opencode / pi)
"""

__version__ = "0.2.0"
