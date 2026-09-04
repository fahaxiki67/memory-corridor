# 记忆回廊（Context Guard Lite 2.0）

一个跨 macOS / Windows 的轻量本地“任务旁记事本 + 完成验收门禁”。它把长任务中最容易丢失的内容放到项目目录里的本地文件：当前要求、验证证据、恢复包和过程经验。AI 需要继续工作时读取恢复包即可，不需要把整段旧对话重新塞回上下文。

## 先说边界

Lite 2.0 是独立 CLI，不直接接管 Codex 的 compact/resume，也不声称自己能自动判断自然语言是否真的完成。用户或 AI 在关键节点调用 CLI：

1. 把要求写入账本；
2. 把实际验证结果绑定到对应要求；
3. 生成短的 recovery packet；
4. 只有 requirement 标记为 `done` 且有当前版本的 `success evidence`，完成门禁才通过。

这正是“旁边有个记事本”的最小可靠版本。未来如果要自动化，再增加 Codex Hook 适配层；核心账本不需要重写。

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

运行时没有第三方依赖；`pyproject.toml` 只为可选的 editable install 提供命令行入口。

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
```

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

### 5. compact/resume 后恢复和完成检查

```text
context-guard recovery packet
context-guard gate check
context-guard gate check --json
```

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

## 五层设计

| 模块 | 责任 |
| --- | --- |
| `contract` | 项目路径、状态文件、开关、原子写入、旁记事本和事件日志 |
| `requirements` | 稳定 ID、类型、状态、revision、更新历史 |
| `evidence` | 把验证摘要绑定到 requirement 当前 revision |
| `recovery` | 从账本和旁记事本生成有边界的恢复包 |
| `gate` | 根据开关、状态和最新 evidence 判断是否通过 |

`cli` 只是入口，不保存另一套状态。数据真相只有 `.context-guard/state.json`；`notebook.md` 面向人读，`events.jsonl` 面向追踪。

## 参考与许可边界

设计上参考了 [GreenLv/codex-context-guard](https://github.com/GreenLv/codex-context-guard) 公开 README 和架构中关于“本地任务账本、bounded recovery、证据绑定、完成门禁”的思路；参考仓库标注为 Apache-2.0。本项目是独立的最小实现，没有复制其源文件、Hook 实现或大段代码。若未来直接移植其代码，应保留 Apache-2.0 许可证、版权/NOTICE 信息，并在修改文件中标明修改；本项目当前不依赖该仓库。

## 测试

Windows：

```powershell
py -3.11 -m unittest discover -s tests -p "test_*.py"
```

macOS：

```bash
python3.11 -m unittest discover -s tests -p 'test_*.py'
```

测试只使用标准库，覆盖初始化、旁记事本、门禁、revision 失效和恢复包。

## 明确不做的事

- 不复制或备份完整聊天记录；
- 不把自然语言“看起来完成”当作证明；
- 不自动替用户修改 requirements；
- 不自动执行命令、安装依赖、上传云端或推送远端；
- 不假装已经接入所有 Codex 生命周期 Hook。

下一步最有价值的增强是增加一个很薄的 Codex Hook adapter：在 compact/resume 前后自动调用 `recovery packet`，在停止前自动调用 `gate check`。这属于外围适配，不应把五层核心重新做重。
