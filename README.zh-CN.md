[English](README.md) | [繁體中文](README.zh-TW.md) | **简体中文**

# AGY 压缩后无损回水插件 (`agy-rehydrate`)

基于 [AGY Post-Compact Rehydration](https://agy-rehydrate.pages.dev/) 封装并增强的 **Google Antigravity（App 2.x 桌面端、CLI `agy` 与 IDE）** 原生独立插件，完整支持 **Windows（PowerShell／CMD）、macOS 与 Linux** 跨平台运行。

全程采用**零第三方依赖**设计：仅需系统 `PATH` 具备 `Python 3.11.2+` 即可开箱即用。

---

## 核心机制与跨端增强

- **平时零额外开销（~1 ms 增量尾扫）**：每次 `PreInvocation` 按字节偏移（`rehydrate_state.json`）增量扫描新增日志；普通对话轮次直接返回 `{}` 零 Token 消耗，仅在刚写入新 `CHECKPOINT`（手动 `/compact` 或跨过 256K 自动压缩）后的首轮执行无损回水，内置存量老会话守卫（`FRESH_CHECKPOINT_MAX_GAP = 8`）与双重幂等保护。
- **零配置自适应回水（`adaptive: true`）**：先过滤有效工作区改动资格再自动切换三档模式（`completed_qa` 纯问答结案／`active_coding` 阶段写码／`mid_turn` 256K 工具循环中途压缩），动态估算标头与前言开销并施加压缩前体积比例上限（极短问答若已被原生摘要完整覆盖则直接静默返回 `{}`）：
  1. **差量早期提问时间线 + 跨多轮里程碑索引**：自动解析原生 `CHECKPOINT`（`# User Requests`），**仅补齐滑出原生 10 条上限或跨 `/fork` 的更早期提问**（零重复；优先折叠上一轮压缩前的原始长日志粘贴并附行号指针）；多轮压缩会话额外提取已从最新摘要消失的早期 `Done` 里程碑／约束并附 `transcript_full.jsonl` 精确行号，抑制跨轮传话衰减。
  2. **预算驱动近期对话滑窗与延迟压缩防护**：`completed_qa` 与 `active_coding` 保留最近 `2` 轮问答、`mid_turn` 留 `3` 轮（失败工具标记 `[ERROR]`）；预算充裕时倒数第二轮回复也全文保留，仅在超额时按剩余预算动态折叠中段；中途自动压缩后若已推进超过 `FRESH_CHECKPOINT_MAX_GAP` 步才触发 Hook，自动跳过过时对话与 `in_progress` 标记避免与现有步骤冲突。
  3. **工作区实态漂移与未注入文件提醒**：在满足三层安全边界前提下，主动检测 `<CONTEXT_SUMMARY>` 或先前步骤引用但磁盘已不存在的过期目录／文件（收敛至顶层缺失目录并附上当前工作区顶层条目）、磁盘外部改动，以及未注入本体的历史修改文件提醒先 `read_file` 再改码。
  4. **活跃规范、技能与编辑锚点视窗（Hunk Window）重读**：在写码或未完成回合重读近期 `SKILL.md`／`PLAN`/`SPEC` 与修改文件的**文件头声明 + 修改区上下 `±45` 行**（支持同压缩视窗内回合数复合时效），并在触及上限时优先整文件退让而非切碎代码块，保持 `## 1..N` 动态连续编号。
- **跨 `/fork` 分支祖先链自动溯源**：最高向上回溯 8 层父会话并严格按 `[min_step, K - 1]` 切片隔离分叉后内容。
- **48 KiB 自动切块与 Fail-Open 保护**：按 `44,000` UTF-8 bytes 自动拆分为多个 `injectSteps` 元素以规避底层单条 `SystemMessageStep` 48 KiB 截断上限；任何异常回退 `{}` 绝不阻塞会话。
- **内置 `/rehydrate` 诊断技能**：支持 `status`、`inspect`、`preview` 与 `show-config` 只读子命令，未触发 `/compact` 前亦可预演回水效果与追踪文件列表。

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
