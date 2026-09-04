# Changelog

本项目所有显著变更都记录在本文件中。

格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，
版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

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
