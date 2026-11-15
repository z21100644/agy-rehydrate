[English](README.md) | **繁體中文** | [简体中文](README.zh-CN.md)

# AGY 壓縮後無損回水插件 (`agy-rehydrate`)

基於 [AGY Post-Compact Rehydration](https://agy-rehydrate.pages.dev/) 封裝並強化的 **Google Antigravity（App 2.x 桌面端、CLI `agy` 與 IDE）** 原生獨立插件，完整支援 **Windows（PowerShell／CMD）、macOS 與 Linux** 跨平台運作。

全程採用**零第三方套件依賴**設計：僅需系統 `PATH` 具備 `Python 3.9+` 即可開箱即用。

---

## 核心機制與跨端強化

- **平時零額外開銷（~1 ms 增量尾掃）**：於每次 `PreInvocation` 以位元組偏移（`rehydrate_state.json`）增量檢查新增日誌；非壓縮輪次直接回傳 `{}` 零 Token 消耗，僅在剛產生新 `CHECKPOINT`（手動 `/compact` 或跨越 256K 自動壓縮）後的首輪觸發無損回水，並具備存量老會話防誤觸（`FRESH_CHECKPOINT_MAX_GAP = 8`）與雙重冪等保護。
- **零配置自適應四層回水（`adaptive: true`）**：自動依壓縮點軌跡切換三種檔位（`completed_qa` 純問答結案／`active_coding` 階段寫碼／`mid_turn` 256K 工具迴圈中途壓縮）並施加壓縮前體積比例護欄（極短問答若已被原生摘要完整覆蓋則直接靜默回傳 `{}`）：
  1. **差量早期提問時間軸**：自動解析剛生成的原生 `CHECKPOINT`（`# User Requests`），**僅補齊超出原生 10 則上限或跨 `/fork` 的更早期提問**（零重複；並自動剝離舊 `[POST-COMPACT REHYDRATION]` 引用文字、過濾早期 `<= 4` 純漢字短句、折疊長貼上）。
  2. **情境自適應近期對話滑窗**：`completed_qa` 與 `active_coding` 保留最近 `2` 輪問答（最後一輪回覆全文保留，倒數第二輪長回覆自動折疊中段、保留結論與開場），`mid_turn` 留 `3` 輪（失敗工具標記 `[ERROR]`）。
  3. **活躍規範與技能文件重掛載**：於寫碼或未完成回合按時效門檻（唯讀 `<= 25` 步、修改中 `PLAN`/`SPEC` `<= 120` 步）即時重讀。
  4. **編輯錨點視窗（Hunk Window）修改檔重讀**：對大型修改檔自動定位 `edit_file` 修改處擷取**檔頭宣告 + 修改區上下 `±45` 行**（取代盲切檔案頭尾），已結束回合唯讀參考檔預設零回塞，並維持三道安全邊界（`status == "DONE"`、`workspacePaths` 邊界、敏感設定與憑證黑名單）與 `## 1..N` 動態連續編號。
- **跨 `/fork` 分支祖先鏈自動溯源**：最高向上回溯 8 層父對話並嚴格以 `[min_step, K - 1]` 切片隔離分叉後內容。
- **48 KiB 自動切塊與 Fail-Open 保護**：按 `44,000` UTF-8 bytes 安全切分多個 `injectSteps` 元素以避免底層單筆 `SystemMessageStep` 48 KiB 截斷；任何異常皆退回 `{}` 絕不阻塞對話。
- **內建 `/rehydrate` 診斷技能**：支援 `status`、`inspect`、`preview` 與 `show-config` 唯讀指令，即使尚未執行 `/compact` 也能預覽將被回水的上下文與追蹤檔案。

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
