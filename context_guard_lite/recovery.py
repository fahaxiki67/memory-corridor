from __future__ import annotations

from pathlib import Path

from .contract import ProjectPaths, atomic_write, load_state, notebook_tail
from .gate import check_gate


def _latest_evidence(state: dict, requirement: dict) -> dict | None:
    matching = [
        item
        for item in state["evidence"]
        if item.get("requirement_id", "").upper() == requirement["id"].upper()
        and item.get("requirement_revision") == requirement.get("revision", 1)
    ]
    return matching[-1] if matching else None


DEFAULT_MAX_DONE_REQUIREMENTS = 20


def build_packet(
    paths: ProjectPaths,
    *,
    max_evidence: int = 10,
    notebook_lines: int = 20,
    max_done_requirements: int = DEFAULT_MAX_DONE_REQUIREMENTS,
) -> str:
    state = load_state(paths)
    gate = check_gate(paths)
    active = [item for item in state["requirements"] if item.get("status") != "superseded"]
    # satisfied = done 且有当前版本 success evidence：这些项对"继续工作"价值最低，
    # 只列最近 N 条并汇总，避免长任务账本把恢复包撑到数千行。
    satisfied_ids = {item["requirement_id"] for item in gate.get("satisfied", [])}
    pending = [item for item in active if item["id"] not in satisfied_ids]
    completed = [item for item in active if item["id"] in satisfied_ids]
    lines = [
        "# 记忆回廊（Context Guard Lite 2.0）Recovery Packet",
        "",
        f"项目：{state['project']['name']}",
        f"生成时间：{state['updated_at']}",
        f"保护状态：{'开启' if state['contract'].get('enabled') else '关闭'}",
        "",
        "## 当前 requirements",
        "",
    ]
    if not pending:
        lines.append("- （暂无待办 requirement）")
    for requirement in pending:
        evidence = _latest_evidence(state, requirement)
        marker = "x" if requirement.get("status") == "done" else " "
        lines.append(
            f"- [{marker}] {requirement['id']} [{requirement['kind']}] "
            f"v{requirement.get('revision', 1)} [{requirement['status']}]: {requirement['text']}"
        )
        if evidence:
            lines.append(f"  - 最新 evidence：{evidence['id']} [{evidence['result']}] {evidence['summary']}")
        else:
            lines.append("  - 最新 evidence：无")

    if completed:
        shown = completed[-max_done_requirements:] if max_done_requirements > 0 else []
        lines.append("")
        lines.append(
            f"## 已完成（{len(completed)} 项已验证，"
            f"列出最近 {len(shown)} 条" + (f"，另有 {len(completed) - len(shown)} 项见 state.json）" if len(completed) > len(shown) else "）")
        )
        for requirement in shown:
            lines.append(
                f"- [x] {requirement['id']} v{requirement.get('revision', 1)}: {requirement['text']}"
            )

    lines.extend(["", "## 最近 evidence", ""])
    recent_evidence = state["evidence"][-max_evidence:] if max_evidence > 0 else []
    if not recent_evidence:
        lines.append("- （暂无 evidence）")
    else:
        for evidence in recent_evidence:
            target = f"；目标：{evidence['target']}" if evidence.get("target") else ""
            command = f"；命令：{evidence['command']}" if evidence.get("command") else ""
            lines.append(
                f"- {evidence['id']} → {evidence['requirement_id']} v{evidence['requirement_revision']} "
                f"[{evidence['result']}] {evidence['summary']}{target}{command}"
            )

    lines.extend(["", "## 旁记事本最近记录", ""])
    tail = notebook_tail(paths, notebook_lines)
    lines.extend(tail or ["（暂无记录）"])
    lines.extend(["", "## Completion Gate", "", f"- 状态：{gate['status']}", f"- 结论：{gate['summary']}"])
    if gate["blocking"]:
        lines.append("- 下一步：处理上面的阻塞项，再重新运行 `context-guard gate check`。")
    else:
        lines.append("- 下一步：可以向用户报告完成，但仍应保留人工判断边界。")
    return "\n".join(lines) + "\n"


def write_packet(
    paths: ProjectPaths,
    *,
    out: str | Path | None = None,
    content: str | None = None,
    max_evidence: int = 10,
    notebook_lines: int = 20,
) -> Path:
    if content is None:
        content = build_packet(paths, max_evidence=max_evidence, notebook_lines=notebook_lines)
    target = paths.recovery if out is None else Path(out)
    if not target.is_absolute():
        target = paths.root / target
    atomic_write(target, content)
    return target
