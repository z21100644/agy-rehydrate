# AGY Post-Compact Rehydration (`agy-rehydrate`)

Lossless post-compaction state rehydration and `/fork` lineage stitching for **Google Antigravity** (Desktop App 2.x, CLI `agy`, and IDE), adapted from [AGY Post-Compact Rehydration](https://agy-rehydrate.pages.dev/).

## What It Restores After `/compact` or 256K Auto-Compaction

On ordinary turns, `PreInvocation` performs a ~1 ms byte-offset tail check and returns `{}` with zero token overhead. Immediately after a new `CHECKPOINT` is written, it self-adaptively classifies the session (`completed_qa` / `active_coding` / `mid_turn`) and injects UTF-8 byte-safe `<48 KiB` `SystemMessage` chunks (or `{}` when a short Q&A is already fully covered by `<CONTEXT_SUMMARY>`):

1. **Delta Earlier User Request Timeline**: Parses native `CHECKPOINT` (`# User Requests`) to restore only earlier prompts omitted by the 10-request summary cap or across `/fork` (zero overlap; strips nested `[POST-COMPACT REHYDRATION]` quotes, filters older `<= 4`-char pure-CJK backchannel turns, and folds long pastes with JSONL line pointers).
2. **Regime-Adaptive Pre-Compaction Dialogue**: Keeps `2` exchanges (`4` entries) in `completed_qa` and `active_coding`, and `3` exchanges (`6` entries) in `mid_turn` (`[ERROR]`-tagged tool trajectories, final Assistant reply verbatim, earlier long Assistant replies middle-condensed).
3. **Restored Active Skills & Spec Docs**: Live disk re-read of recently accessed custom `SKILL.md` (`<= 25` steps) and active `*SPEC*` / `*PLAN*` / `*TEMPLATE*` / `*HANDOFF*` Markdown files during coding or mid-turn sessions.
4. **Edit-Hunk Windowed Workspace Files**: Extracts file headers + `±45` lines around `edit_file` anchors for large modified files instead of blind head+tail clipping; skips completed-turn read-only files (`maxReferencedFiles: 0`, restoring `[StartLine..EndLine]` slices only in `mid_turn`), gated by `status == "DONE"`, active `workspacePaths` containment, and sensitive credential/config denylisting with sequential `## 1..N` headings.

Use `/rehydrate` in chat to inspect session readiness, preview restored context, or customize `.agents/rehydrate.json` budgets.
