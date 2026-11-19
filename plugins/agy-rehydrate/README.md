# AGY Post-Compact Rehydration (`agy-rehydrate`)

Lossless post-compaction state rehydration and `/fork` lineage stitching for **Google Antigravity** (Desktop App 2.x, CLI `agy`, and IDE), adapted from [AGY Post-Compact Rehydration](https://agy-rehydrate.pages.dev/).

## What It Restores After `/compact` or 256K Auto-Compaction

On ordinary turns, `PreInvocation` performs a ~1 ms byte-offset tail check and returns `{}` with zero token overhead. Immediately after a new `CHECKPOINT` is written, it self-adaptively classifies the session (`completed_qa` / `active_coding` / `mid_turn`) and injects UTF-8 byte-safe `<48 KiB` `SystemMessage` chunks (or `{}` when a short Q&A is already fully covered by `<CONTEXT_SUMMARY>`):

1. **Delta Earlier User Request Timeline & Anti-Decay Milestone Index**: Parses native `CHECKPOINT` (`# User Requests`) to restore only earlier prompts omitted by the 10-request summary cap or across `/fork` (zero overlap; folds pre-window raw log pastes first with JSONL line pointers) and recovers dropped `Done` milestones / constraints from earlier checkpoints with exact `transcript_full.jsonl` line pointers.
2. **Budget-Driven Pre-Compaction Dialogue**: Keeps `2` exchanges (`4` entries) in `completed_qa` and `active_coding`, and `3` exchanges (`6` entries) in `mid_turn` (`[ERROR]`-tagged tool trajectories); preserves earlier Assistant replies verbatim when budget allows and scales middle-folding dynamically on overflow, while omitting stale pre-checkpoint dialogue on delayed mid-turn auto-compactions (`delayed_gap > 8`).
3. **Live Workspace Reality & File Status Notice**: Flags stale/missing workspace paths still referenced in `<CONTEXT_SUMMARY>` (with live top-level workspace entries), external disk edits since the last touch, and uninjected modified files requiring `read_file` before editing — all strictly gated by the 3 security boundaries.
4. **Restored Active Skills, Specs & Edit-Hunk Windowed Files**: Live disk re-read of recently accessed custom `SKILL.md` (`<= 25` steps), active `*SPEC*`/`*PLAN*`/`*TEMPLATE*`/`*HANDOFF*` docs, and file headers + `±45` lines around `edit_file` anchors for eligible workspace files (`completed_qa` mode is gated on actual eligible modifications; cap overflow trims whole trailing file blocks cleanly before clipping) with sequential `## 1..N` headings.

Use `/rehydrate` in chat to inspect session readiness, preview restored context, or customize `.agents/rehydrate.json` budgets.
