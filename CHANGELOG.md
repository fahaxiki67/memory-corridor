# Changelog

本项目所有显著变更都记录在本文件中。

格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，
版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

## [2.9.4] - 2026-09-18

### Changed

- `events list` 对无法解析的 JSONL 行在 stderr 输出明确告警，同时保持 stdout 的有效事件机器可读。
- `read_events` 以可选出参返回跳过行数；既有调用方保持兼容，损坏或截断事件日志不再与空日志无提示地混淆。

### Verification boundary

- PR CI 覆盖 Ubuntu、macOS、Windows 与 Python 3.11–3.14；本版为小版本修复发布。
- Release 不宣称替代真实客户端验收；ZCode/Claude/Codex 集成仍以 README 中各自的验证边界为准。

## [2.9.3] - 2026-09-13

### 文档与验证记录

- **Windows 真机多轮复测通过（v2.9.2 本体）**：headless CLI + 本地 Anthropic 协议 mock，连续 3 轮独立会话行为完全一致——英文路径 open 账本（注入 → block → 续命 → 防循环 allow）、补齐 evidence 后（注入 → PASS allow）、含中文与空格路径（注入 → block → 防循环 allow）。
- README 补充：升级/卸载后旧会话 hook 报错的预期行为（fail-open）与「复制新版本目录为旧版本名作兼容别名」的静默方案。

### 无代码变更

本版仅为文档与验证记录更新；插件行为与 2.9.2 完全一致。

## [2.9.2] - 2026-09-12

### 修复

- **恢复包自限 20000 字符（24KB 截断实锤后的防御）**：核对 ZCode 客户端源码确认 SessionStart 注入在 **24000 字符**处被 `truncateForHook` 静默截尾（超限内容直接丢失结尾的 Completion Gate 指引）。恢复包条目数本有界，但单条 requirement/evidence/note 文本无上限，极端账本仍可能超限。`recovery.build_packet` 现按信息价值降级自限：先省略旁记事本（占位指向 notebook.md）→ 省略已完成段 → evidence 只留前 3 条 → 超长行截到 200 字符 → 兜底保留包头与 Completion Gate 段。新增 2 项测试覆盖降级顺序与端到端上限，总测试 121 项。

### 验证更新（Windows 真机全链路闭环）

- **Stop 门禁全链路已在 Windows 真机验证**（2026-09-12，ZCode CLI 0.16.5 / Win10 x64）：headless CLI + 本地 Anthropic Messages 协议 mock 服务驱动**真实模型回合**，events.jsonl 完整记录 `SessionStart injected → Stop block（stop_hook_active=false）→ 续命 → allow（stop_hook_active=true）→ PASS allow（gate_status=pass）`——与 macOS 桌面端结论一致。README 记录了复现方法。
- **含中文与空格的项目路径**：hook 链路（SessionStart 注入 + Stop 门禁）在同一真机实测通过。

## [2.9.1] - 2026-09-12

### 修复（重要：Windows 用户请升级）

- **ZCode Plugin hook 在 Windows 上开箱即用**：`hooks/hooks.json` 从 `type:"process"` + 固定 `python3` 命令改为 `type:"command"` shell 命令 `python3 … || py -3 …`——macOS/Linux 走原生 `python3`；Windows 上 `python3` 不可用（或解析到 Store 存根非零退出）时自动回退官方 `py -3` launcher。`||` 与双引号在 cmd.exe 与 POSIX sh 下语义一致，一份配置双平台生效，Windows 用户不再需要手工改 hooks.json。
- Windows 真机验证（2026-09-12，ZCode CLI 0.16.5 / Windows 10 x64 / Python 3.13）：跨平台 hook 命令成功拉起 wrapper，SessionStart 注入恢复包（events.jsonl `result=injected`）；`zcode plugins list` 显示 `hooks: 2`。119 项测试全绿。

### 变更

- `memory-corridor zcode status`：`python3_on_path` 字段升级为 `hook_interpreter_on_path`（任一解释器可用即 True）与 `hook_interpreters_on_path`（逐个解释器可用性明细）；hooks.json 自检从「匹配固定命令名」改为识别 wrapper 引用（同时兼容 process 型旧安装与 command 型本版），不再误报「未配置」。
- 运行条件输出同步改为按解释器逐个展示可用性。

### 边界（如实）

- **Stop 门禁真机验证仍为 macOS 桌面客户端级别**（2026-09-12，见 2.9.0）：Windows 真机已完成 hook 启动链路验证（SessionStart 注入），Stop 的 block→续命→PASS 全链路在 Windows 上待人工按 README「Windows 安装与验证」步骤 6–7 复核（headless CLI 缺 API key 凭据，无法自动化驱动完整回合）。

## [2.9.0] - 2026-09-11

### 新增

- **ZCode 原生 Plugin 集成（实验性）**：新增 `context_guard_lite/integrations/zcode.py` 协议适配层与 Plugin 分发文件（`.zcode-plugin/plugin.json`、`hooks/hooks.json`、`hooks/zcode_hook.py`、`.zcode-marketplace/marketplace.json`），在 Codex/Claude 适配之外支持 ZCode 的两个关键事件：
  - **SessionStart**（matcher `startup|resume|compact`）：未初始化项目 no-op 不落文件；已初始化则从最新 `state.json` 现场重建 Recovery Packet 并经 `hookSpecificOutput.additionalContext` 注入；`clear` 视为用户主动清空上下文，不注入；从不读旧 `recovery.md`。
  - **Stop**：自动运行完成门禁。未初始化/保护关闭/空账本/gate pass → 放行；blocked → 返回 `{"decision":"block","reason":<精简阻塞清单>}`；`stop_hook_active` 为真后不再阻塞（本适配每回合最多请求 1 次续命，远低于 ZCode 平台 3 次硬上限）；state 损坏按「无法读取 ≠ 通过」阻塞一次。
- 新 CLI 子命令 `memory-corridor zcode hook` / `zcode status [--json]`（ZCode 的 hook 由 Plugin 分发，**没有** install/uninstall 命令，不写项目配置文件）。
- 新增测试 30 项（协议契约、放行空输出、防续命循环、损坏 state、中文路径与空格、非法 JSON、不支持事件、wrapper 子进程、Plugin 文件结构、状态输出），总测试 119 项。

### 变更

- 协议事实以 ZCode 官方文档与客户端实现核对（2026-09-11）：ZCode 仅支持 7 种 hook 事件（**无 PreCompact**）；hook stdout 是严格 schema（多余键整份作废）；**Stop 上 `continue:true` 会触发续命**（与 Codex/Claude 相反），因此 ZCode 放行输出为空而不是 `{"continue":true}`。
- 五层核心（contract/requirements/evidence/recovery/gate）与 Codex/Claude 集成零改动；`.context-guard/state.json` 仍是唯一业务状态来源。

### 边界（如实）

- **SessionStart 已在真实 ZCode CLI（0.16.5，headless `--prompt`）端到端验证**（2026-09-11）：插件经 `plugins list` 显示 hooks:2，真实会话触发 SessionStart 并注入恢复包（events.jsonl `platform=zcode … result=injected`）；卸载后 hooks 消失、重复安装幂等（条目不重复）。
- **Stop 门禁已在真实 ZCode 桌面客户端端到端验证**（2026-09-12）：`decision=block`（stop_hook_active=false）→ 客户端续命 → 防循环放行（stop_hook_active=true）→ gate PASS 放行，全链路有 events.jsonl `platform=zcode` 记录。仍待人工：Windows 上 `python3` 别名可用性、24KB 恢复包截断、本地 marketplace GUI 安装交互。

## [2.8.0] - 2026-09-05

### 修复（重要：建议所有用户升级）

- **并发写丢失更新（P0）**：v2.7.0 及更早版本中，state.json 的写入是"读—改—原子替换"，没有跨进程文件锁；hook、终端、脚本同时写同一账本会互相覆盖（实测 30 个并发 `requirements add` 只剩 25~28 条，且 ID 会重号、后写覆盖前写）。v2.8.0 新增 `locking.py`：跨平台文件锁（POSIX `fcntl` / Windows `msvcrt`，零依赖，10 秒超时抛错不挂死 hook）+ 锁内读改写事务 `state_transaction`，全部六个 state 写路径（requirements add / import / update（含 done）、evidence add、note add、contract on/off）改造为事务。实测 30 并发 add 零丢失零重号。

### 新增

- **完成门禁 idle 状态**：空账本不再阻塞。此前新用户 init + hook install 后每一回合都被 `no_requirements` 阻塞；现在空账本返回 `status: idle`，Stop hook 放行并附一句"如何记录第一条 requirement"的引导。曾经有需求、后来全部 superseded 时仍阻塞（`all_superseded`）。
- **hook 触发事件可观测**：已初始化项目里每次 hook 触发都在 events.jsonl 追加 `hook.stop` / `hook.pre_compact` / `hook.session_start`（含 platform、decision、gate_status、blocking_count、stop_hook_active），`memory-corridor events list --type hook.stop` 可直接验证门禁是否真的在工作。未初始化项目仍保持 no-op 不落文件；审计写入失败不影响门禁决策。
- **`gate check` 退出码按状态区分**：pass / idle / disabled → 0，blocked → 1，脚本可分辨"未配置"与"未通过"。

### 变更

- README 的 Claude Code 章节与 `claude install` 输出明确标注**实验性**：协议实现基于官方文档并有集成测试覆盖，但尚未在真实 Claude Code 会话中完成端到端验收。
- README"并发与规模边界"章节更新：单写者假设自本版本起不再成立（写入已加跨进程锁），历史实测数据保留存档。
- CI：并发回归测试只在 ubuntu-latest × Python 3.12 全量运行，其余 11 格以 `MC_SKIP_CONCURRENCY=1` 跳过，避免慢速 runner 拖垮矩阵。

### 测试

- 新增并发回归 2 项（20 并发 add 零丢失零重号；req / evidence / note 混合并发写全量保真）；idle 门禁与退出码 3 项；hook 事件可观测 8 项；总测试 89 项。

## [2.7.0] - 2026-09-05

### 新增

- **Claude Code 原生集成**（`memory-corridor claude hook/install/status/uninstall`）：与 Codex 集成共用同一事件处理器，写入 `<项目>/.claude/settings.json`（project scope）。
  - 用户配置保护：settings.json 里你自己的 permissions、MCP、第三方 hook 原样保留；install 幂等；非法 JSON 拒改；单份 `.bak` 滚动备份；
  - Claude handler 保持 `type/command/timeout` 最小字段集合（不写 Codex 专属的 statusMessage/additionalContextLimit）；
  - status 含 matcher 漂移检测；trust 如实提示"工作区信任对话框管控，无公开接口自动判定"。
- 重构：抽出通用 hook 配置引擎 `integrations/hook_config.py`，Codex 与 Claude 共用合并/卸载/状态检查逻辑；Codex 公开 API 与行为零变化。

### 平台差异（按官方文档实现）

- Claude Code 无逐 hook trust hash：以工作区信任对话框管控（`-p` 非交互模式视为已信任）；
- PreCompact 的 `systemMessage` 会被 Claude Code 丢弃（恢复包落盘副作用不受影响）；
- Stop 连续阻塞 8 次后平台强制结束回合；本工具的 `stop_hook_active` 防循环在其之前生效。

### 测试

- 新增 10 项 Claude 集成测试（配置管理对称套件 + hook 协议复用），总测试 77 项；Codex 侧 67 项回归无变化。

## [2.6.0] - 2026-09-05(https://keepachangelog.com/zh-CN/1.1.0/)，
版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

## [2.6.0] - 2026-09-05

### 新增

- `requirements done Rxxx`：`update --status done` 的快捷方式，标记后自动按门禁口径检查证据——缺少当前版本 success evidence 时明确警告并给出补证据命令，杜绝"标了完成却过不了门禁"的常见遗忘。不产生任何 evidence、不改变门禁语义。
- 新增 `examples/agents-snippet.md`：给 AI 助手的指令片段（粘贴进项目 `AGENTS.md`），让 AI 在长任务中主动记录要求、真实验证后记账、遵守 Stop 门禁——配合本工具把"AI 长任务不丢要求"真正落地。

### 测试

- 新增 2 项测试（done 糖的无证据警告与满足提示、revision 升级后要求补新证据），总测试 67 项。

## [2.5.0] - 2026-09-05(https://keepachangelog.com/zh-CN/1.1.0/)，
版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

## [2.5.0] - 2026-09-05

### 新增

- `requirements import <file|->`：从 UTF-8 文本批量导入 requirements（每行一条，空行与 `#` 注释行跳过；容忍 BOM；`-` 表示 stdin）。批量导入只保存一次 state，避免写放大；事件日志逐条带 `imported: true` 标记，保持审计粒度。
- `recovery packet --max-done N`：把 v2.3.1 引入的已完成项折叠上限暴露到 CLI（默认 20，`0` 表示只显示汇总）。
- 新增 `ruff` 静态检查（CI 独立 lint job；仅开发期工具，运行时保持零依赖）。

### 修复

- `requirements import -` 在 stdin 为二进制流时不再把字节串当作文本导入（与 `codex hook` 相同的 UTF-8 兼容处理）。

### 测试

- 新增 7 项测试（批量导入解析/BOM/管道/文件缺失、--max-done 折叠边界），总测试 65 项；全库通过 ruff 检查。

## [2.4.0] - 2026-09-05(https://keepachangelog.com/zh-CN/1.1.0/)，
版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

## [2.4.0] - 2026-09-05

本版延续实验数据复查：events.jsonl 双进程并发追加（各 200 行，401/401 全部合法零丢失——追加日志比账本状态抗并发）、5000 条账本极限（gate 16ms / packet 36ms，性能无虞）、200 条阻塞项输出量化。

### 新增

- `gate check` 文本输出默认只列前 20 个阻塞项并汇总提示，`--all` 查看全部；`--json` 保持全量不变（实验实测 200 条阻塞时文本输出 19KB / 601 行，终端不可用）。
- `codex status --json`：安装状态的结构化输出（hooks 文件有效性、三事件配置、PATH 检查、初始化状态、trust 提示），供脚本与工具读取。
- `status` 新增恢复包新鲜度：显示"已生成（UTC 时间）"或"未生成"，`--json` 增加字段 `recovery_generated_at`。

### 测试

- 新增 3 项测试（gate check 截断与 --all/--json 全量、status 恢复包新鲜度、codex status --json），总测试 61 项。

## [2.3.1] - 2026-09-05(https://keepachangelog.com/zh-CN/1.1.0/)，
版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

## [2.3.1] - 2026-09-05

本版来自一轮实验数据复查：大规模账本（500 requirements / 1000 evidence / 500 notes）、极端文本（100KB 单条 / emoji / 控制字符）、200 条阻塞项的 Stop reason、带 BOM 的 hooks.json、双进程并发写。

### 修复

- **恢复包分层折叠**：`recovery packet` / SessionStart 注入包中，"done 且有当前版本 success evidence"的项只列最近 20 条并汇总（`…另有 N 项见 state.json`）；未满足项（open / blocked / 证据不合格的 done）保持全量列出。实验实测 300 完成 + 200 待办的账本，恢复包从约 60KB 降至 20KB，待办零丢失。`build_packet` 新增可选参数 `max_done_requirements`（默认 20）。
- **hooks.json 容忍 UTF-8 BOM**：读取使用 `utf-8-sig`，Windows 记事本等编辑器加 BOM 后不再被误判为非法 JSON；写入始终无 BOM。

### 文档

- README 新增"并发与规模边界"：单写者假设（实验：双进程并发各写 50 条丢失约一半）、恢复包分层规则、SessionStart 注入 4000 tokens 上限与 Codex spill 行为。

### 测试

- 新增 3 项测试（折叠汇总与待办保全、证据不合格的 done 不折叠、BOM 容忍），总测试 58 项。

## [2.3.0] - 2026-09-05

### 新增

- 账本查询增强（账本变大后的按需过滤，全部为可选参数，向后兼容）：
  - `requirements list --kind must --status open`；
  - `evidence list --for R001`（只看绑定到某个 requirement 的证据）；
  - `note list --kind experience --source ai`。
- 新增只读审计命令 `events list`：查询追加式事件日志 `events.jsonl`，支持 `--limit N` 与 `--type requirement.add` 按类型过滤；跳过无法解析的行，绝不修改文件。
- `codex status` 新增配置漂移检测：PreCompact/SessionStart 的 matcher 被手动改动后，status 明确警告"已偏离安装值"，避免 Hook 触发条件静默变化。

### 变更

- CI 矩阵加入 Python 3.14（3 操作系统 × 4 版本 = 12 job），并增加 `concurrency` 取消同分支过时运行；`classifiers` 同步声明 3.14。

### 测试

- 新增 6 项测试（三类 list 过滤、events 过滤与未初始化拒绝、matcher 漂移检测），总测试 55 项。

## [2.2.1] - 2026-09-05

### 新增

- `codex install` 现在会检查 `memory-corridor` 是否在 PATH 上，找不到时输出明确警告（Hook 触发会报 command not found 的最常见原因）。
- Stop 阻塞清单加上限（前 10 个阻塞项 + 汇总提示"… and N more blocked requirements not listed"），大量 requirement 未完成时 continuation reason 仍然精简，落实"只发送 active blocker、不发全量 state"的边界。
- `codex hook` 检测到 stdin 是交互终端（TTY）时直接拒绝并提示正确用法，避免误运行挂住等待输入。

### 测试

- 新增 4 项边界测试（blocker 上限截断、上限内完整列出、TTY stdin 拒绝、install 的 PATH 警告开关），总测试 49 项。

## [2.2.0] - 2026-09-04

### 新增

- Codex 原生 Hook 集成（`context_guard_lite/integrations/codex.py`，薄适配层）：
  - `memory-corridor codex hook`：统一 Hook 入口，stdin 接收 Codex Hook JSON，按 `hook_event_name` dispatch，stdout 只输出合法 Hook JSON，诊断走 stderr；
  - `PreCompact`：在 cwd 已初始化时刷新 `.context-guard/recovery.md`；未初始化时按约定 no-op，不创建任何文件；
  - `SessionStart`（matcher `resume|compact`）：用最新 state 重建 Recovery Packet 并通过 `hookSpecificOutput.additionalContext` 注入；`startup`/`clear` 不注入；
  - `Stop`：调用现有完成门禁。gate blocked 且 `stop_hook_active=false` 时返回 `decision=block` 加精简阻塞清单；`stop_hook_active=true` 时不再续命（防无限循环），改用 `systemMessage` 提示；未初始化/disabled/pass 均正常放行；
  - 状态损坏时绝不把"无法判断"伪装为 pass，也不伪造 Recovery Packet。
- `memory-corridor codex install` / `codex status` / `codex uninstall`（仅 project scope：`<项目>/.codex/hooks.json`）：
  - install 幂等；已有第三方 Hook 完整保留，只追加 Memory Corridor 自己的 entries；
  - 写入前生成单份滚动备份 `hooks.json.bak`（不无限生成）；
  - 原文件不是合法 JSON 时拒绝操作、不覆盖；
  - uninstall 只移除 Memory Corridor 的 Hook；
  - status 如实显示 `trust: unable to determine automatically` 并提示 `/hooks` 人工确认；不绕过 Codex hook trust。
- 新增测试文件 `tests/test_codex_integration.py`：Hook 行为 15 场景、协议错误处理、配置管理幂等与第三方保护共 30 项。

### 明确不做（本轮边界）

- 不做 PostToolUse 自动 evidence、不做 transcript 解析、不用 LLM 判断 requirement 是否完成、不自动修改 requirement；
- 不默认改全局 `~/.codex/hooks.json` 或 config.toml；
- 五层核心（contract/requirements/evidence/recovery/gate）业务语义零改动，`SCHEMA_VERSION` 保持 1。

## [2.1.0] - 2026-09-04

### 新增

- CLI 增加 `--version`，可打印当前版本号。
- 补齐开源发布要素：MIT `LICENSE`、本 `CHANGELOG.md`、GitHub Actions CI（Ubuntu / macOS / Windows × Python 3.11–3.13 矩阵）。
- `pyproject.toml` 补全发布元数据：readme、license、authors、keywords、classifiers、项目 URLs。
- 新增边界测试：重复 `init` 报错、空账本门禁阻塞、failed evidence 阻塞、空笔记报错、恢复包自定义输出路径、`--version`。

### 修复

- `recovery packet` 不再重复构建恢复包两次（`write_packet` 支持直接接收已生成的文本）。
- `atomic_write` 由模块私有改为公开：`recovery.py` 跨模块使用它写入自定义路径。

### 变更

- 版本号在 `pyproject.toml` 与 `context_guard_lite.__version__` 两处保持一致（2.1.0）。

## [2.0.0] - 2026-08-31

### 新增

- 首个公开版本：本地任务账本（requirements / evidence / notes）、旁记事本 `notebook.md`、追加式事件日志 `events.jsonl`、恢复包 `recovery packet`、完成门禁 `gate check`。
- requirement 带 revision 与历史：修改文本或类型后，旧 evidence 不再自动适用于新版本。
- 纯标准库实现，零第三方依赖，跨 macOS / Windows 同一套项目目录结构。
