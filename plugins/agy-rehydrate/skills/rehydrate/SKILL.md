---
name: rehydrate
description: >-
  Inspect post-compaction context rehydration status, preview what user prompts,
  verbatim turns, /fork lineage, specs, and workspace files will be restored after
  /compact or 256K auto-compaction, and configure .agents/rehydrate.json budgets.
  Use when the user invokes /rehydrate or asks about context compaction recovery.
---

# Post-Compact Rehydration (`/rehydrate`)

`agy-rehydrate` runs automatically via `PreInvocation` with ~1 ms incremental tail checks on normal turns and triggers **self-adaptive** lossless recovery (`completed_qa` / `active_coding` / `mid_turn`, `CHECKPOINT` `# User Requests` delta deduplication, and `edit_file` hunk windowing) on the **first turn immediately after `/compact` or 256K auto-compaction** across Antigravity 2.0 App, CLI, and IDE.

## Read-Only Diagnostic CLI

Run [rehydrate_after_compact.py](../../scripts/rehydrate_after_compact.py) via `run_command` (resolve `<plugin-dir>` relative to this `SKILL.md`, e.g. `~/.gemini/config/plugins/agy-rehydrate`):

```bash
# 1. List recent App / CLI / IDE sessions, step ranges, /fork origins, and checkpoint state
python -B "<plugin-dir>/scripts/rehydrate_after_compact.py" --cwd "<workspace>" status

# 2. Dry-run rehydration stats, /fork ancestors, and tracked files (simulates next /compact if uncompacted)
python -B "<plugin-dir>/scripts/rehydrate_after_compact.py" --cwd "<workspace>" inspect latest

# 3. Preview rendered rehydration header & sections (add --full for complete markdown)
python -B "<plugin-dir>/scripts/rehydrate_after_compact.py" --cwd "<workspace>" preview latest

# 4. Show active character & file slot budgets (merged defaults + .agents/rehydrate.json)
python -B "<plugin-dir>/scripts/rehydrate_after_compact.py" --cwd "<workspace>" show-config
```

You can pass a specific `<conversation-id>` (or UUID prefix) instead of `latest`.

## Optional Workspace Tuning (`.agents/rehydrate.json`)

To customize rehydration budgets or disable automatic injection for a specific workspace, create `<workspace>/.agents/rehydrate.json` (or `~/.agents/rehydrate.json` globally):

```json
{
  "enabled": true,
  "adaptive": true,
  "budgetAllUserChars": 24000,
  "budgetRecentTurnsChars": 14000,
  "budgetSkillsSpecsChars": 12000,
  "budgetFilesTotalChars": 24000,
  "budgetSingleFileChars": 10000,
  "maxRecentEntries": 6,
  "maxModifiedFiles": 3,
  "maxReferencedFiles": 0,
  "maxModifiedAgeSteps": 60
}
```
