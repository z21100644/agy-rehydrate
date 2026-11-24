[English](README.md) | [繁體中文](README.zh-TW.md) | **简体中文**

# AGY 压缩后无损回水插件 (`agy-rehydrate`)

基于 [AGY Harness Hook Tricks: Post-Compact Rehydration](https://agy-harness-hook-tricks.pages.dev/compact-rehydration) 封装并增强的 **Google Antigravity（App 2.x 桌面端、CLI `agy` 与 IDE）** 原生独立插件，完整支持 **Windows（PowerShell／CMD）、macOS 与 Linux** 跨平台运行。

全程采用**零第三方依赖**设计：仅需系统 `PATH` 具备 `Python 3.11.2+` 即可开箱即用。

---

## 核心机制与架构设计（`v2.2.0`）

`<CONTEXT_SUMMARY>` 属于有损摘要，而本地 `transcript_full.jsonl` 与磁盘上的工作区文件才是无损真相。

- **平时零额外开销（~1 ms 增量尾扫）**：每次 `PreInvocation` 以 `python -S -B` / `python3 -S -B` 执行（快速路径仅导入 `json`、`os`、`sys`），按字节偏移（`rehydrate_state.json`）增量扫描日志；普通对话轮次直接返回 `{}` 零 Token 消耗，仅在刚写入新 `CHECKPOINT`（手动 `/compact` 或回合中途 256K／512K 自动压缩）后的首轮触发无损回水，内置存量老会话防误触守卫（`FRESH_CHECKPOINT_MAX_GAP = 8`）、未回复自动压缩续行宽限（`MAX_AUTOCOMPACT_TURN_GAP = 400`）与双重幂等保护。
- **确定性四层固定配额回水**：以可预期、可配置的四层结构取代易漂移的自适应／NLP 摘要启发式规则：
  1. **完整用户提问时间线**（`budgetAllUserChars = 24,000`）：跨 `/fork` 分支编号还原完整提问时间线；第 1 条与最近 75 条提问优先全文保留（超额时按预算折叠），过滤更早期的 `<= 4` 字符纯非 ASCII／非数字寒暄，并对已包含于第 2 层或早于近期窗口的 `> 800` 字符长粘贴进行折叠且保留章节或 `transcript_full.jsonl` 行号指针。
  2. **压缩前近期逐字对话**（`budgetRecentTurnsChars = 28,000`、`maxSingleTurnChars = 12,000`）：由新到旧回填逐字 User／Assistant 对话与单行去重工具轨迹（含压缩前尚未完成的工具调用），跨切块时保证单轮原子性绝不截断。
  3. **活跃技能、规范文档与仓库规则指针**（`budgetSkillsSpecsChars = 14,000`）：列出所触及 Git／项目根目录下的 `AGENTS.md`／`GEMINI.md` 路径指针，并从磁盘实时重读已触及的 `SKILL.md`／`*SPEC*`／`*PLAN*`／`*TEMPLATE*`／`*HANDOFF*` 文档。
  4. **活跃工作区文件实时重读**（`budgetFilesTotalChars = 28,000`、`budgetSingleFileChars = 8,000`）：从当前压缩周期（`fileScopeSincePrevCheckpoint = true`，聚合主会话与 `/swarm` worker／子代理文件触及记录）实时重读最多 5 个修改文件与 2 个参考文件，对大文件精确提取编辑／读取行窗口、对未定位巨型单文件提供精简结构摘要；严格 Fail-Closed 限定于工作区根目录（可选全局 `extraAllowedPaths`），解析符号链接并拦截 `.env*`、私钥、二进制文件、系统路径与 `scratch/`。
- **跨 `/fork` 分支祖先链自动溯源**：最高向上回溯 8 层父会话并严格按 `[min_step, K - 1]` 切片隔离分叉后内容。
- **48 KiB 自动切块与 Fail-Open 保护**：按 `44,000` UTF-8 bytes 配合 CommonMark 围栏安全裁剪与单轮原子边界拆分为多个 `injectSteps` 元素，避免触发底层单条 `SystemMessageStep` 48 KiB 截断上限；任何异常回退 `{}` 绝不阻塞会话。
- **内置 `/rehydrate` 诊断技能**：支持 `status`、`inspect`、`preview` 与 `show-config` 只读子命令（支持 `--cwd`、`--checkpoint`、`--limit`、`--full`），未触发 `/compact` 前亦可预演回水效果与追踪文件列表。

---

## 安装与卸载

- **Windows**：
  ```bat
  install.bat      & rem 安装至 %USERPROFILE%\.gemini\config\plugins\agy-rehydrate
  uninstall.bat    & rem 卸载插件
  ```

- **macOS / Linux**：
  ```sh
  ./install.sh     # 安装至 ~/.gemini/config/plugins/agy-rehydrate
  ./uninstall.sh   # 卸载插件
  ```

安装后无需重启客户端，可在 Antigravity App 左侧 **Skills & Customizations** 面板随时独立开关；对 `minimal-coder`（`excludeDefaultComponents: true`）底层 `PreInvocation` Hook 同样免改配置自动生效。

---

## 工作区自定义配额（`.agents/rehydrate.json`）

如需针对特定项目关闭回水或调整各层预算，可创建 `<workspace>/.agents/rehydrate.json`（或全局 `~/.agents/rehydrate.json`）：

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

全局专属安全配置（仅 `~/.agents/rehydrate.json` 生效，工作区配置无法覆写或放宽）：
- `"extraAllowedPaths": ["~/path/to/trusted/dir"]`：允许第 4 层额外重读指定的非工作区可信目录（仍受敏感文件、二进制与系统路径过滤保护）。
- 全局配置 `"enabled": false` 时，工作区的 `.agents/rehydrate.json` 无法将其覆写为 `true`。
