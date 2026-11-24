# AGY Post-Compact Rehydration (`agy-rehydrate`)

Deterministic 4-layer post-compaction state rehydration and `/fork` lineage stitching for **Google Antigravity** (Desktop App 2.x, CLI `agy`, and IDE), based on [AGY Harness Hook Tricks: Post-Compact Rehydration](https://agy-harness-hook-tricks.pages.dev/compact-rehydration).

## Design Principle

`<CONTEXT_SUMMARY>` is lossy, while local `transcript_full.jsonl` and workspace files on disk are lossless ground truth. On ordinary turns, `PreInvocation` runs a ~1 ms in-process incremental byte-offset tail check (`python -S -B` / `python3 -S -B`, importing only `json`, `os`, `sys`) and returns `{}` with zero token overhead. Immediately after a new `CHECKPOINT`, it injects 4 predictable, fixed-budget layers split into `< 48 KiB` UTF-8 chunks:

1. **Complete User Request Timeline** (`budgetAllUserChars = 24,000`): Numbered full user prompt timeline across `/fork` branches; prompt #1 and the last 75 prompts are kept verbatim (subject to budget folding), older `<= 4`-char non-ASCII/non-digit backchannels are filtered, and `> 800`-char pastes that are already included in Layer 2 or older than the recent window are folded with section or `transcript_full.jsonl` line pointers.
2. **Recent Pre-Compaction Verbatim Dialogue** (`budgetRecentTurnsChars = 28,000`, `maxSingleTurnChars = 12,000`): Reverse-walked verbatim User/Assistant exchanges with single-line tool trajectories, never split mid-turn across chunks.
3. **Restored Active Skills, Spec Docs & Repo Rule Pointers** (`budgetSkillsSpecsChars = 14,000`): `AGENTS.md`/`GEMINI.md` path pointers at touched git or project roots plus live disk reads of touched `SKILL.md` / `*SPEC*`/`*PLAN*`/`*TEMPLATE*`/`*HANDOFF*` files.
4. **Restored Active Workspace Files** (`budgetFilesTotalChars = 28,000`, `budgetSingleFileChars = 8,000`): Live disk reads of Top 5 modified + Top 2 referenced workspace files from the current compaction cycle (`fileScopeSincePrevCheckpoint = true`, aggregating `/swarm` worker and subagent file touches), using exact active edit/read line windows on oversized files and compact structural summaries on unlocated monoliths, strictly scoped to workspace roots (plus optional global-only `extraAllowedPaths`), blocking `.env*`, private keys, binaries, and `scratch/`.

Use `/rehydrate` in chat to inspect session status, preview rehydrated output, or tune `.agents/rehydrate.json` budgets.
