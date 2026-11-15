**English** | [繁體中文](README.zh-TW.md) | [简体中文](README.zh-CN.md)

# AGY Post-Compact Rehydration (`agy-rehydrate`)

Standalone native plugin for **Google Antigravity** (Desktop App 2.x, CLI `agy`, and IDE), adapted from [AGY Post-Compact Rehydration](https://agy-rehydrate.pages.dev/) with full cross-platform support (**Windows / macOS / Linux**) and multi-surface compatibility.

Zero third-party dependencies — requires only `Python 3.14+` in `PATH`.

---

## Core Capabilities

- **Zero Overhead on Normal Turns (~1 ms)**: Uses byte-offset incremental tail scanning (`rehydrate_state.json`) during `PreInvocation` and immediately returns `{}` unless a fresh `CHECKPOINT` (`/compact` or 256K auto-compaction) just occurred.
- **Self-Adaptive 4-Layer State Recovery (`adaptive: true`)**: Automatically classifies compaction context into three regimes (`completed_qa` / `active_coding` / `mid_turn` 256K mid-tool-loop) with proportional pre-compaction size caps (and silently returns `{}` when a short Q&A is already 100% covered by `<CONTEXT_SUMMARY>`):
  1. **Delta Earlier User Request Timeline**: Parses native `CHECKPOINT` (`# User Requests`) so `## 1` **only restores earlier prompts omitted by the native 10-request window or across `/fork`**, eliminating duplication while stripping nested `[POST-COMPACT REHYDRATION]` quotes, filtering older `<= 4`-char pure-CJK backchannel turns, and folding `> 800`-char pastes with line pointers.
  2. **Regime-Adaptive Pre-Compaction Dialogue**: Keeps `2` exchanges (`4` entries) in `completed_qa` and `active_coding`, and `3` exchanges (`6` entries) in `mid_turn` (`[ERROR]`-tagged tool trajectories, final Assistant reply verbatim, earlier long Assistant replies middle-condensed).
  3. **Restored Active Skills & Spec Docs**: Re-reads recently accessed (`status == "DONE"`, `<= 25` steps read-only, `<= 120` steps modified) custom `SKILL.md` and active `*SPEC*` / `*PLAN*` / `*TEMPLATE*` / `*HANDOFF*` Markdown files during coding or mid-turn sessions.
  4. **Edit-Hunk Windowed Workspace Files**: Extracts file headers + `±45` lines around `edit_file` anchors for large modified files instead of blind head+tail clipping, skips completed-turn read-only files (`maxReferencedFiles: 0`, restoring `[StartLine..EndLine]` slices only in `mid_turn`), and enforces 3-gate security (`status == "DONE"`, `workspacePaths` containment, sensitive credential/config denylist) with sequential `## 1..N` headings.
- **Cross-`/fork` Lineage Stitching**: Automatically traces parent conversation segments up to 8 hops with strict step-range isolation (`[min_step, K - 1]`).
- **48 KiB Chunking & Fail-Open Safety**: Splits injected `SystemMessage` blocks under `44,000` UTF-8 bytes to prevent truncation at Antigravity's 48 KiB step cap, and guarantees Fail-Open `{}` output on any unexpected exception.
- **`/rehydrate` Diagnostic Skill**: Built-in `status`, `inspect`, `preview`, and `show-config` CLI subcommands to inspect sessions or dry-run rehydration before running `/compact`.

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
