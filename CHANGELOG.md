# Changelog

## 0.2.0 — 2026-09-15

First release intended for a second machine.

### Layout
- The curator writes nothing into the skills tree any more. Telemetry, scheduler state, the
  audit ledger and its blobs, archived skills and whole-tree snapshots live under
  `<state>/trees/<key>/`, keyed by the tree's resolved path.
- A snapshot is therefore purely the skills; restoring one never rewinds telemetry or the ledger.
- Ledger entries are validated against the skills tree and its archive, which fixes single-entry
  rollback under the real Herdr layout (it was always refused when skills lived outside the
  state dir).
- `CURATOR_HOME` is only a state-dir override; the self-contained layout is gone.
- Cron-job protection and reference rewriting are removed (nothing produced those jobs).
- Auto-registered harness skill dirs are recorded in `<state>/registered_skill_dirs.json`
  instead of being appended to the user's `config.json`.

### Telemetry hooks
- Host config files are rewritten in place: a symlinked `settings.json` keeps its link and file
  mode is preserved.
- OpenCode and pi installers refuse to overwrite a plugin file that is not ours.
- The launcher exits silently when `python3` is not on the host's PATH.
- The Claude Code matcher is anchored and names `MultiEdit` and `NotebookEdit` explicitly; a
  matcher change counts as a stale install, so the startup reconcile refreshes it.
- The receiver decides project-file paths from the skill roots without walking the trees.

### Daemon
- `curator startup` outside Herdr no longer spawns a daemon; a daemon with no socket path and
  no `HERDR_ENV` runs one tick and exits.
- After a plugin reinstall the daemon re-execs itself from the new checkout.

### Docs
- Herdr has no `plugin update`: reinstall is the upgrade path. Removal steps are documented in
  order, including that archived skills are deleted with the state dir.
- Notes on dotfile managers.

## 0.1.0

Initial plugin: usage telemetry, stale/archive lifecycle, snapshots, audit ledger with rollback,
opt-in LLM consolidation pass, telemetry hooks for Claude Code, Codex, OpenCode and pi.
