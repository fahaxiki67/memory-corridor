# Memory Corridor 使用指令（AI 助手片段）

把下面这段粘贴进项目的 `AGENTS.md`（或 Codex / 其他助手的持久指令区），AI 就会在长任务中主动维护账本。请按项目实际取舍删改。

---

## 任务账本纪律（Memory Corridor）

本项目使用 Memory Corridor（命令 `memory-corridor`，别名 `context-guard`）维护任务账本与完成门禁。你必须遵守：

### 何时记账

1. 用户提出新的硬性要求、约束或验收标准时，**立即**记录，不要等项目结束再补：
   - `memory-corridor requirements add "<要求原文（可精简但不得改变语义）>" --kind must|avoid|acceptance`
   - `must` = 必须完成；`avoid` = 禁止或必须保持不变；`acceptance` = 需要独立验证的验收项。
2. 完成一项工作后，**先验证、后记账**：
   - 运行真实的验证（测试、构建、人工核对），然后
     `memory-corridor evidence add --for Rxxx --summary "<验证了什么>" --result success --command "<实际执行的验证命令>"`
   - 只有验证确实通过才能记 `success`；失败记 `failed`，不确定记 `unknown`。
   - 证据确认后标记完成：`memory-corridor requirements done Rxxx`
     （如果缺少当前版本的成功证据，该命令会明确警告，此时去补证据，不要谎报完成。）
3. 修改 requirement 文本或类型会使旧证据失效（revision 升级）——改完后必须重新验证并补新证据。
4. 过程中得到可复用的经验、教训、用户拍板的决定时：
   - `memory-corridor note add "<内容>" --kind experience|decision|lesson --source ai|user`

### 红线

- **禁止**在没有真实验证的情况下记录 `success`；命令退出码 0 不等于 requirement 满足。
- **禁止**替用户发明、删除或悄悄修改 requirement；改口径必须留痕（`update --reason`）。
- **禁止**为了通过门禁绕过、篡改或删除 `.context-guard/` 下的任何数据。

### Stop 门禁被阻塞时

Stop Hook 返回的 blocked 清单里列出了未满足的 requirement。正确动作：
1. 逐项处理清单上的工作；
2. 补充真实证据；
3. 用 `requirements done Rxxx` 标记完成后再次结束。
不要反复空转尝试结束——第二次 Stop 不会再阻塞，但门禁状态不会因此变绿。

### 关键节点（已由 Hook 自动化）

- compact 前：恢复包自动写入 `.context-guard/recovery.md`；
- resume / compact 后：最新恢复包自动注入你的上下文——里面的 requirements 与证据就是当前工作状态，优先以其为准；
- 这些自动化不覆盖上面的记账义务。
