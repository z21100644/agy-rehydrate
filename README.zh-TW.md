[English](README.md) | **繁體中文** | [简体中文](README.zh-CN.md)

# AGY 壓縮後無損回水插件 (`agy-rehydrate`)

基於 [AGY Harness Hook Tricks: Post-Compact Rehydration](https://agy-harness-hook-tricks.pages.dev/compact-rehydration) 封裝並強化的 **Google Antigravity（App 2.x 桌面端、CLI `agy` 與 IDE）** 原生獨立插件，完整支援 **Windows（PowerShell／CMD）、macOS 與 Linux** 跨平台運作。

全程採用**零第三方套件依賴**設計：僅需系統 `PATH` 具備 `Python 3.11.2+` 即可開箱即用。

---

## 核心機制與架構設計（`v2.2.0`）

`<CONTEXT_SUMMARY>` 屬於有損摘要，而本機 `transcript_full.jsonl` 與磁碟上的工作區檔案才是無損真相。

- **平時零額外開銷（~1 ms 增量尾掃）**：於每次 `PreInvocation` 以 `python -S -B` / `python3 -S -B` 執行（快速路徑僅載入 `json`、`os`、`sys`），依位元組偏移（`rehydrate_state.json`）增量掃描日誌；非壓縮輪次直接回傳 `{}` 零 Token 消耗，僅在剛產生新 `CHECKPOINT`（手動 `/compact` 或回合中途 256K／512K 自動壓縮）後的首輪觸發無損回水，並具備存量老會話防誤觸（`FRESH_CHECKPOINT_MAX_GAP = 8`）、未回覆自動壓縮續行寬限（`MAX_AUTOCOMPACT_TURN_GAP = 400`）與雙重冪等保護。
- **確定性四層固定配額回水**：以可預期、可設定的四層結構取代易漂移的自適應／NLP 摘要啟發式規則：
  1. **完整使用者提問時間軸**（`budgetAllUserChars = 24,000`）：跨 `/fork` 分支編號還原完整提問時間軸；第 1 則與最近 75 則提問優先全文保留（超額時按預算折疊），過濾更早期的 `<= 4` 字元純非 ASCII／非數字寒暄，並對已包含於第 2 層或早於近期視窗的 `> 800` 字元長貼上進行折疊且保留章節或 `transcript_full.jsonl` 行號指標。
  2. **壓縮前近期逐字對話**（`budgetRecentTurnsChars = 28,000`、`maxSingleTurnChars = 12,000`）：由新至舊回填逐字 User／Assistant 對話與單行去重工具軌跡（含壓縮前尚未結束的工具呼叫），跨切塊時保證單輪原子性絕不攔腰切斷。
  3. **活躍技能、規範文件與倉庫規則指標**（`budgetSkillsSpecsChars = 14,000`）：列出所觸及 Git／專案根目錄下的 `AGENTS.md`／`GEMINI.md` 路徑指標，並從磁碟即時重讀已觸及的 `SKILL.md`／`*SPEC*`／`*PLAN*`／`*TEMPLATE*`／`*HANDOFF*` 文件。
  4. **活躍工作區檔案即時重讀**（`budgetFilesTotalChars = 28,000`、`budgetSingleFileChars = 8,000`）：自當前壓縮週期（`fileScopeSincePrevCheckpoint = true`，彙整主對話與 `/swarm` worker／子代理檔案觸及紀錄）即時重讀最多 5 個修改檔與 2 個參考檔，對大型檔案精準擷取編輯／讀取行視窗、對未定位巨型單檔提供精簡結構摘要；嚴格 Fail-Closed 限定於工作區根目錄（可選全域 `extraAllowedPaths`），解析符號連結並攔截 `.env*`、私鑰、二進位檔、系統目錄與 `scratch/`。
- **跨 `/fork` 分支祖先鏈自動溯源**：最高向上回溯 8 層父對話並嚴格以 `[min_step, K - 1]` 切片隔離分叉後內容。
- **48 KiB 自動切塊與 Fail-Open 保護**：按 `44,000` UTF-8 bytes 搭配 CommonMark 圍欄安全裁切與單輪原子邊界拆分為多個 `injectSteps` 元素，避免觸及底層單筆 `SystemMessageStep` 48 KiB 截斷上限；任何異常皆退回 `{}` 絕不阻塞對話。
- **內建 `/rehydrate` 診斷技能**：支援 `status`、`inspect`、`preview` 與 `show-config` 唯讀指令（支援 `--cwd`、`--checkpoint`、`--limit`、`--full`），尚未執行 `/compact` 前也能預覽將被回水的上下文與追蹤檔案。

---

## 安裝與移除

- **Windows**：
  ```bat
  install.bat      & rem 安裝至 %USERPROFILE%\.gemini\config\plugins\agy-rehydrate
  uninstall.bat    & rem 移除插件
  ```

- **macOS / Linux**：
  ```sh
  ./install.sh     # 安裝至 ~/.gemini/config/plugins/agy-rehydrate
  ./uninstall.sh   # 移除插件
  ```

安裝完成後無需重啟客戶端，可隨時於 Antigravity App 左側 **Skills & Customizations** 面板單獨開關；對 `minimal-coder`（`excludeDefaultComponents: true`）底層 `PreInvocation` Hook 亦免改設定自動生效（若需在 `minimal-coder` 內呼叫 `/rehydrate` 技能，於 `agent.md` frontmatter `skills:` 加入 `rehydrate` 即可）。

---

## 工作區自訂配額（`.agents/rehydrate.json`）

如需針對特定專案停用回水或調整各層字元預算，可建立 `<workspace>/.agents/rehydrate.json`（或全域 `~/.agents/rehydrate.json`）：

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

全域專屬安全設定（僅 `~/.agents/rehydrate.json` 生效，工作區設定無法覆寫或放寬）：
- `"extraAllowedPaths": ["~/path/to/trusted/dir"]`：允許第 4 層額外重讀指定的非工作區可信目錄（仍受機密、二進位與系統路徑過濾保護）。
- 全域設定 `"enabled": false` 時，工作區的 `.agents/rehydrate.json` 無法將其覆寫為 `true`。
