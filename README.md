# 记忆回廊（Context Guard Lite 2.0）

[![CI](https://github.com/fahaxiki67/memory-corridor/actions/workflows/ci.yml/badge.svg)](https://github.com/fahaxiki67/memory-corridor/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
![Python](https://img.shields.io/badge/Python-3.11%2B-blue)

一个跨 macOS / Windows 的轻量本地“任务旁记事本 + 完成验收门禁”。它把长任务中最容易丢失的内容放到项目目录里的本地文件：当前要求、验证证据、恢复包和过程经验。AI 需要继续工作时读取恢复包即可，不需要把整段旧对话重新塞回上下文。

## 先说边界

Lite 2.0 是独立 CLI，不直接接管 Codex 的 compact/resume，也不声称自己能自动判断自然语言是否真的完成。用户或 AI 在关键节点调用 CLI：

1. 把要求写入账本；
2. 把实际验证结果绑定到对应要求；
3. 生成短的 recovery packet；
4. 只有 requirement 标记为 `done` 且有当前版本的 `success evidence`，完成门禁才通过。

这正是“旁边有个记事本”的最小可靠版本。v2.2.0 起通过一层薄的 Codex Hook 适配把同样的动作接入了 Codex 的 compact/resume 与停止阶段（见下方“Codex 原生集成”）；核心账本保持独立，没有 Codex 也照常可用。

## 目录结构

运行 `init` 后，项目目录会出现：

```text
.context-guard/
├── state.json       # 机器可读的当前账本
├── events.jsonl     # 追加式事件记录
├── notebook.md      # 人可以直接打开编辑的旁记事本
└── recovery.md      # 最近一次恢复包
```

默认 `.gitignore` 忽略 `.context-guard/`，避免把任务文本或内部记录误提交。若确实需要版本化旁记事本，删除 `.gitignore` 中对应行后再提交。

状态写入采用同目录临时文件加替换，避免进程中断时半写入 `state.json`。文本统一 UTF-8；路径使用 Python `pathlib`，不依赖 bash、PowerShell 特有语法或系统级服务，因此 Windows 和 macOS 使用同一份项目目录结构。

## Windows 开始使用

在本项目目录下，使用 Python Launcher：

```powershell
py -3.11 -m context_guard_lite init --name "我的长任务"
py -3.11 -m context_guard_lite requirements add "保留现有 API" --kind must
py -3.11 -m context_guard_lite requirements add "必须运行基础测试" --kind acceptance
py -3.11 -m context_guard_lite status
```

如果命令行已经安装了本项目，也可以直接使用 `context-guard`：

```powershell
memory-corridor init --name "我的长任务"
```

`context-guard` 仍保留为兼容别名；下文示例使用它时，替换为 `memory-corridor` 也完全相同。

不安装也能运行，直接使用 `py -3.11 -m context_guard_lite` 是最小方式。

## macOS 使用

同一项目目录下，把解释器命令换成 `python3.11`：

```bash
python3.11 -m context_guard_lite init --name "我的长任务"
python3.11 -m context_guard_lite status
```

可选的本地安装方式（只安装到虚拟环境，不污染全局 Python）：

先取得源码：

```bash
git clone https://github.com/fahaxiki67/memory-corridor.git
cd memory-corridor
```

Windows：

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\python -m pip install -e .
```

macOS：

```bash
python3.11 -m venv .venv
.venv/bin/python -m pip install -e .
```

运行时没有第三方依赖；`pyproject.toml` 只为可选的 editable install 提供命令行入口。查看版本：

```text
memory-corridor --version
```

## 常用流程

### 1. 初始化、开关和状态

```text
context-guard init --name "重构任务"
context-guard on
context-guard off
context-guard status
context-guard status --json
```

`init` 默认开启保护。`off` 只关闭门禁，不删除任何记录；`on` 可以重新开启。

### 2. 记录和更新 requirements

```text
context-guard requirements add "不得修改数据库结构" --kind avoid
context-guard requirements add "新增两个测试" --kind acceptance
context-guard requirements list
context-guard requirements update R001 --status done
context-guard requirements update R002 --text "新增两个单元测试" --reason "用户补充了验收口径"
context-guard requirements import tasks.txt --kind must
```

批量初始化账本可用 `requirements import`：每行一条要求，空行与 `#` 注释行跳过，`-` 表示从 stdin 读入；导入只在最后保存一次 state，事件日志逐条带 `imported` 标记。

类型只有三种：

- `must`：必须完成的要求；
- `avoid`：禁止或必须保持不变的约束；
- `acceptance`：需要独立验证的验收项。

修改文本或类型会提升 revision，旧 evidence 不会自动证明新版本；历史保留在 `state.json`。`superseded` 用于明确不再适用的旧项，建议同时新增替代 requirement。

### 3. 记录 evidence

```text
context-guard evidence add \
  --for R002 \
  --summary "测试全部通过" \
  --result success \
  --command "py -3.11 -m unittest discover -s tests"

context-guard evidence list
```

`success`、`failed`、`unknown` 必须显式填写。只有绑定到 requirement 当前 revision 的 `success`，才可能通过门禁；孤立的“命令执行成功”不会自动关闭任何要求。

### 4. 写入经验和过程记录

```text
context-guard note add "先找所有调用方，再改共享函数" --kind experience --source ai
context-guard note add "API 兼容性是本任务硬约束" --kind decision --source user
context-guard note list --limit 20
```

这些记录会同时进入 `state.json` 和可直接打开的 `notebook.md`。程序只保存、检索和展示，不会把经验未经确认地改写成硬性 requirement；这样“经验”与“用户要求”不会混在一起。

所有 `list` 命令都支持按需过滤：`requirements list --kind must --status open`、`evidence list --for R001`、`note list --kind experience --source ai`。追加式事件日志也可以只读审计：

```text
context-guard events list --limit 20
context-guard events list --type requirement.add
```

### 5. compact/resume 后恢复和完成检查

```text
context-guard recovery packet
context-guard gate check
context-guard gate check --json
context-guard gate check --all
```

阻塞项很多时，文本输出默认只列前 20 条并汇总提示；`--all` 查看全部，`--json` 输出全量结构化结果供程序读取。`status` 同样支持 `--json`，并会显示恢复包是否已生成及其生成时间。

`recovery packet` 会输出并保存一份短恢复包，包含 active requirements、每项当前 evidence、最近 evidence、旁记事本尾部和完成规则。它是给人或 AI 随时调取的“工作记忆”，不是完整聊天记录。

完成门禁要求：

```text
保护开启
+
所有 active requirements 状态为 done
+
每项都有当前 revision 的最新 success evidence
→ gate pass
```

若最新 evidence 是 `failed` 或 `unknown`，即使更早有 `success`，也会阻塞，直到补充新的成功验证。

## 示例

[examples/task.md](examples/task.md) 是一个完整的最小示例：为小项目补充 CSV 导出功能，从初始化账本、记录 evidence 到最后通过门禁的全过程命令。

[examples/agents-snippet.md](examples/agents-snippet.md) 是给 AI 助手的指令片段：粘贴进项目的 `AGENTS.md` 后，AI 会在长任务中主动记录要求、验证并记账（配合 `requirements done` 的证据检查，AI 无法谎报完成）。

`requirements done R001` 是 `update --status done` 的快捷方式，并会自动检查证据：缺少当前版本的 success evidence 时明确警告并给出补证据命令，避免"标了完成却过不了门禁"的常见遗忘。

## Codex 原生集成

v2.2.0 起提供一层薄的 Codex Hook 适配（`context_guard_lite/integrations/codex.py`），在 context compact/resume 和停止阶段自动调用上面的五层能力。适配层只做协议翻译：`.context-guard/state.json` 仍是唯一业务真相，不会自动产生 evidence，不会解析 transcript，也不会替你把 requirement 改成 done。

三个 Hook 均调用同一条命令 `memory-corridor codex hook`（stdin JSON 进、stdout JSON 出），因此 macOS / Windows 使用同一份配置。

### 安装

在项目目录（需要已 `pip install` 本项目，保证 `memory-corridor` 在 PATH 上）：

```text
memory-corridor codex install
```

它只会写入当前项目的 `.codex/hooks.json`（project scope），不改全局配置。生成：

| Hook | matcher | 行为 |
| --- | --- | --- |
| `PreCompact` | `manual\|auto` | 刷新 `.context-guard/recovery.md`（未初始化项目 no-op，不创建文件） |
| `SessionStart` | `resume\|compact` | 用最新 state 重建 Recovery Packet 并注入 `additionalContext` |
| `Stop` | （无） | 检查完成门禁，blocked 时把精简阻塞清单作为 continuation 送回 |

已有第三方 Hook 的项目请放心：install 会解析现有 JSON、完整保留第三方 entries、只追加自己的 Hook，写入前生成单份滚动备份 `hooks.json.bak`；原文件不是合法 JSON 时直接拒绝、不覆盖。重复执行 install 是幂等的。

其他命令：

```text
memory-corridor codex status     # 检查配置、初始化状态、command 是否在 PATH
memory-corridor codex uninstall  # 只移除 Memory Corridor 的 Hook，第三方保留
```

### /hooks trust

安装完成 ≠ 已经生效。Codex 对项目级非托管 Hook 有 trust/review 机制，信任之前 Hook 会被静默跳过：

1. 在项目目录启动 `codex`；
2. 运行 `/hooks` 查看；
3. review 并 trust Memory Corridor 的三个 Hook。

程序不会绕过 Codex 的 hook trust，也不会替你改 trust 状态；`codex status` 里 trust 一栏显示 `unable to determine automatically`，请以 `/hooks` 的人工确认为准。

### compact/resume 工作流

```text
（compact 发生前）PreCompact → recovery.md 落盘，UI 提示已刷新
（resume / compact 后）SessionStart → 最新 Recovery Packet 注入新上下文
```

注意两点设计边界：`startup` 与 `clear` 不注入（用户可能就是想清空上下文）；恢复包始终由最新 state 现场重建，不读可能过期的 recovery.md。恢复的是结构化工作状态，不是聊天历史。

### Stop completion gate

模型尝试结束回合时，Stop Hook 调用 `gate check`：

- 项目未初始化 / 保护已关闭 / gate pass → 正常放行，不影响 Codex 结束；
- gate blocked → 返回 `decision: "block"`，reason 是精简的可执行清单（形如 `R001 (v1): - 状态为 open，不是 done`），Codex 会把它作为 continuation 继续推进；
- blocked 且 `stop_hook_active=true`（本轮已被续过一次）→ 不再 block，改用 `systemMessage` 提示门禁仍未满足，避免无限循环。

门禁的可信边界不变：Hook 只会阻塞“账本说没完成”的结束，永远不会自动证明“已完成”。

### 排障

- Hook 完全没运行：先跑 `memory-corridor codex status`；确认在 Codex 里 `/hooks` 已 trust；确认 `memory-corridor` 在 PATH 上（venv 用户需在激活 venv 的终端里启动 codex）；
- `hooks.json` 被判断为非法 JSON：install/uninstall 会拒绝操作并保留现场，请先修复或用 `hooks.json.bak` 还原；
- state.json 损坏：Hook 不会把“读不了”当 pass——Stop 首次会阻塞并提示检查 `.context-guard/state.json`；请保留现场手工修复，程序不自动改写；
- 想临时停用但保留记录：`memory-corridor off`（Stop Hook 会改为放行）。

## Claude Code 原生集成

Claude Code 的 hooks 与 Codex 同源，v2.7.0 起提供对称集成，事件处理器完全复用：

```text
memory-corridor claude install     # 写入 <项目>/.claude/settings.json（project scope）
memory-corridor claude status [--json]
memory-corridor claude uninstall
memory-corridor claude hook        # Hook 统一入口（与 codex hook 同一处理器）
```

行为与差异要点：

- PreCompact / SessionStart(`resume|compact`) / Stop 三个 Hook 的行为与 Codex 版一致：刷新恢复包、注入 `additionalContext`、Stop 门禁阻塞与防循环；
- **用户配置保护**：`.claude/settings.json` 里你自己的 permissions、MCP 配置、第三方 hook 等内容原样保留；install 幂等，uninstall 只移除 Memory Corridor 的 hook；
- **信任机制**：Claude Code 用工作区信任对话框管控项目 Hook（信任前不运行），`/hooks` 菜单只读查看；没有 Codex 那种逐 hook trust hash；
- `claude -p` 非交互模式视为已信任，适合自动化验收；
- PreCompact 的 `systemMessage` 会被 Claude Code 丢弃（恢复包落盘副作用不受影响）；handler 保持 `type/command/timeout` 最小字段集合；
- Stop 连续阻塞 8 次后 Claude Code 会强制结束回合（平台内置兜底）；我们的 `stop_hook_active` 防循环在其之前就已生效。

## 五层设计

| 模块 | 责任 |
| --- | --- |
| `contract` | 项目路径、状态文件、开关、原子写入、旁记事本和事件日志 |
| `requirements` | 稳定 ID、类型、状态、revision、更新历史 |
| `evidence` | 把验证摘要绑定到 requirement 当前 revision |
| `recovery` | 从账本和旁记事本生成有边界的恢复包 |
| `gate` | 根据开关、状态和最新 evidence 判断是否通过 |

`cli` 只是入口，不保存另一套状态。数据真相只有 `.context-guard/state.json`；`notebook.md` 面向人读，`events.jsonl` 面向追踪。

### 并发与规模边界

以下均为实验实测数据（2026-09，M4 / Python 3.14）：

- **单写者假设**：state.json 的写入是“读—改—原子替换”，没有跨进程文件锁。两个进程同时写同一账本会互相覆盖（实验实测：双进程各写 50 条会丢失约一半）。Codex Hook 与人工 CLI 都请保持同一时刻只有一个写者。`events.jsonl` 是纯追加日志，实测双进程并发各追加 200 行零丢失零损坏。
- **恢复包分层**：`recovery packet` 中**未满足的 requirement 永远全量列出**（open、blocked、done 但证据不合格）；只有“done 且有当前版本 success evidence”的项折叠为最近 20 条 + 汇总，长任务账本（数百项）下恢复包体积从数万字符降一个量级。
- **SessionStart 注入上限**：Hook 安装的 `additionalContextLimit` 为 4000 tokens。账本极大时恢复包超出部分会由 Codex 的 spill 机制落盘供模型按需读取，这是 Codex 的设计内行为。
- **实测规模上限参考**：5000 条 requirements 下 `gate check` 16ms、恢复包构建 36ms；`events.jsonl` 10 万行（15.5MB）下 `events list` 全量解析 117ms；`notebook.md` 5MB 下尾读 6ms；requirement 500 次修订后 state.json 仅 110KB（history 线性增长，供审计）。日常规模远小于此。

## 设计归属

记忆回廊是 fahaxiki67 个人主导、独立实现的跨 macOS / Windows 本地任务账本工具。项目代码、数据格式、CLI 和完成门禁均为本项目自有实现，运行时不依赖其他项目。

设计上参考了开源社区关于长任务持久化、恢复包、验证证据和完成门禁的公开实践；相关思路可见 [GreenLv/codex-context-guard](https://github.com/GreenLv/codex-context-guard)。

## 测试

Windows：

```powershell
py -3.11 -m unittest discover -s tests -p "test_*.py"
```

macOS：

```bash
python3.11 -m unittest discover -s tests -p 'test_*.py'
```

测试只使用标准库，覆盖初始化、旁记事本、门禁、revision 失效、恢复包和常见输入错误。CI 在 Ubuntu / macOS / Windows × Python 3.11–3.13 矩阵上运行同一套测试，并额外验证 `pip install .` 后两个命令行入口可用。

## License

本项目以 [MIT License](LICENSE) 发布。

## 明确不做的事

- 不复制或备份完整聊天记录；
- 不把自然语言“看起来完成”当作证明；
- 不自动替用户修改 requirements；
- 不自动执行命令、安装依赖、上传云端或推送远端；
- 不假装已经接入所有 Codex 生命周期 Hook（当前只接 PreCompact / SessionStart(resume|compact) / Stop 三个）；
- 不绕过 Codex 的 hook trust 机制。

Codex Hook 适配已作为外围薄层落地（v2.2.0），五层核心没有被做重；后续增强同样只应发生在外围。
