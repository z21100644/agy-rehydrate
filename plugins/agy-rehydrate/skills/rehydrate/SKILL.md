---
name: rehydrate
description: >-
  Inspect post-compaction context rehydration status, preview what user prompts,
  verbatim turns, /fork lineage, specs, and workspace files will be restored after
  /compact or 256K auto-compaction, and configure .agents/rehydrate.json budgets.
  Use when the user invokes /rehydrate or asks about context compaction recovery.
---

# Post-Compact Rehydration (`/rehydrate`)

`agy-rehydrate` runs via `PreInvocation` with ~1 ms in-process incremental tail checks on normal turns and triggers deterministic 4-layer recovery (complete user timeline, recent verbatim dialogue, active skills/specs + repo rule pointers, and live top modified/referenced workspace files, plus `/fork` lineage stitching) on the **first turn immediately after `/compact` or 256K auto-compaction** across Antigravity 2.x App, CLI, and IDE.

## Read-Only Diagnostic CLI

Run [rehydrate_after_compact.py](../../scripts/rehydrate_after_compact.py) via `run_command` (resolve `<plugin-dir>` relative to this `SKILL.md`, e.g. `~/.gemini/config/plugins/agy-rehydrate`; use `python3` instead of `python` on macOS/Linux hosts where `python` is unavailable):

```bash
# 1. List recent App / CLI / IDE sessions, step ranges, /fork origins, and checkpoint state (optional --limit N, default 8)
python -B "<plugin-dir>/scripts/rehydrate_after_compact.py" status --limit 8

# 2. Dry-run rehydration stats, /fork ancestors, and tracked files (simulates next /compact if uncompacted; optional --checkpoint N)
python -B "<plugin-dir>/scripts/rehydrate_after_compact.py" --cwd "<workspace>" inspect latest

# 3. Preview rendered rehydration header & sections (add --full for complete markdown, or --checkpoint N for a specific checkpoint)
python -B "<plugin-dir>/scripts/rehydrate_after_compact.py" --cwd "<workspace>" preview latest

# 4. Show active character & file slot budgets (merged defaults + .agents/rehydrate.json)
python -B "<plugin-dir>/scripts/rehydrate_after_compact.py" --cwd "<workspace>" show-config
```

Pass a specific `<conversation-id>` (or UUID prefix, or explicit JSONL path) instead of `latest`, pass `--checkpoint N` to `inspect`/`preview` for a specific checkpoint step, pass `--limit N` to `status` to adjust how many sessions are listed, and repeat `--cwd` (before or after the subcommand) for multi-root workspaces (first `--cwd` takes highest `.agents/rehydrate.json` precedence).

## Optional Workspace Tuning (`.agents/rehydrate.json`)

To customize budgets or disable automatic injection for a specific workspace, create `<workspace>/.agents/rehydrate.json` (or `~/.agents/rehydrate.json` globally):

```json
{
  "enabled": true,
  "budgetAllUserChars": 24000,
  "budgetRecentTurnsChars": 28000,
  "budgetSkillsSpecsChars": 14000,
  "budgetFilesTotalChars": 28000,
  "budgetSingleFileChars": 8000,
  "maxSingleTurnChars": 12000,
  "maxModifiedFiles": 5,
  "maxReferencedFiles": 2,
  "fileScopeSincePrevCheckpoint": true
}
```

Global-only option in `~/.agents/rehydrate.json` (ignored in workspace `.agents/rehydrate.json` for security):
- `"extraAllowedPaths": ["~/path/to/trusted/dir"]` — allow Layer 4 live disk reads from explicit non-workspace directories (still subject to secret/binary/system-path filters).
- Setting `"enabled": false` in `~/.agents/rehydrate.json` cannot be overridden to `true` by a workspace config.
