**English** | [繁體中文](README.zh-TW.md) | [简体中文](README.zh-CN.md)

# AGY Post-Compact Rehydration (`agy-rehydrate`)

Standalone native plugin for **Google Antigravity** (Desktop App 2.x, CLI `agy`, and IDE), adapted from [AGY Harness Hook Tricks: Post-Compact Rehydration](https://agy-harness-hook-tricks.pages.dev/compact-rehydration) with full cross-platform support (**Windows / macOS / Linux**) and multi-surface compatibility.

Zero third-party dependencies — requires only `Python 3.11.2+` in `PATH`.

---

## Core Capabilities (`v2.1.0`)

`<CONTEXT_SUMMARY>` is lossy, while local `transcript_full.jsonl` and workspace files on disk are lossless ground truth.

- **Zero Overhead on Normal Turns (~1 ms)**: Runs `python -S -B` / `python3 -S -B` during `PreInvocation` (importing only `json`, `os`, `sys`) with incremental byte-offset tail scanning (`rehydrate_state.json`), returning `{}` with zero token overhead unless a fresh `CHECKPOINT` (`/compact` or 256K auto-compaction) just occurred (`FRESH_CHECKPOINT_MAX_GAP = 8` guard and dual idempotency protection).
- **Deterministic 4-Layer State Recovery**: Predictable, fixed-budget layers without fragile adaptive/NLP summary heuristics:
  1. **Complete User Request Timeline** (`budgetAllUserChars = 24,000`): Numbered full user prompt timeline across `/fork` branches; prompt #1 and the last 75 prompts are kept verbatim (subject to budget folding), older `<= 4`-char non-ASCII/non-digit backchannels are filtered, and `> 800`-char pastes that are already included in Layer 2 or older than the recent window are folded with section or `transcript_full.jsonl` line pointers.
  2. **Recent Pre-Compaction Verbatim Dialogue** (`budgetRecentTurnsChars = 28,000`, `maxSingleTurnChars = 12,000`): Reverse-walked verbatim User/Assistant exchanges with single-line deduplicated tool trajectories (plus pre-compaction in-progress tools), never split mid-turn across chunks.
  3. **Restored Active Skills, Spec Docs & Repo Rule Pointers** (`budgetSkillsSpecsChars = 14,000`): `AGENTS.md` / `GEMINI.md` path pointers at touched git or project roots plus live disk reads of touched `SKILL.md` / `*SPEC*` / `*PLAN*` / `*TEMPLATE*` / `*HANDOFF*` files.
  4. **Restored Active Workspace Files** (`budgetFilesTotalChars = 28,000`, `budgetSingleFileChars = 8,000`): Live disk reads of Top 5 modified + Top 2 referenced workspace files from the current compaction cycle (`fileScopeSincePrevCheckpoint = true`), strictly fail-closed to workspace roots (plus optional global-only `extraAllowedPaths`), resolving symlinks and blocking `.env*`, private keys, binaries, system paths, and `scratch/`.
- **Cross-`/fork` Lineage Stitching**: Automatically traces parent conversation segments up to 8 hops with strict step-range isolation (`[min_step, K - 1]`).
- **48 KiB Chunking & Fail-Open Safety**: Splits injected `SystemMessage` blocks under `44,000` UTF-8 bytes with CommonMark fence-safe clipping and turn-atomic chunk boundaries, and guarantees Fail-Open `{}` output on any unexpected exception.
- **`/rehydrate` Diagnostic Skill**: Built-in `status`, `inspect`, `preview`, and `show-config` read-only CLI subcommands (supporting `--cwd`, `--checkpoint`, `--limit`, and `--full`) to inspect sessions or dry-run rehydration before running `/compact`.

---

## Installation & Removal

- **Windows**:
  ```bat
  install.bat
  uninstall.bat
  ```

- **macOS / Linux**:
  ```sh
  ./install.sh
  ./uninstall.sh
  ```

Once installed to `~/.gemini/config/plugins/agy-rehydrate`, you can toggle the plugin live at any time from **Skills & Customizations** in the Antigravity App sidebar without restarting.

---

## Optional Workspace Configuration (`.agents/rehydrate.json`)

Create `<workspace>/.agents/rehydrate.json` (or `~/.agents/rehydrate.json` globally) to customize budgets or disable injection per project:

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

Global-only security controls in `~/.agents/rehydrate.json` (ignored or cannot be loosened by workspace configs):
- `"extraAllowedPaths": ["~/path/to/trusted/dir"]` — allow Layer 4 live disk reads from explicit non-workspace directories (still subject to secret/binary/system-path filters).
- `"enabled": false` in `~/.agents/rehydrate.json` cannot be overridden to `true` by a workspace `.agents/rehydrate.json`.
