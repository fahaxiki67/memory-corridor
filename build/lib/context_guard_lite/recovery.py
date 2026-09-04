from __future__ import annotations

from pathlib import Path

from .contract import GuardError, ProjectPaths, atomic_write, load_state, notebook_tail
from .gate import check_gate


def _latest_evidence(state: dict, requirement: dict) -> dict | None:
    matching = [
        item
        for item in state["evidence"]
        if item.get("requirement_id", "").upper() == requirement["id"].upper()
        and item.get("requirement_revision") == requirement.get("revision", 1)
    ]
    return matching[-1] if matching else None


def build_packet(paths: ProjectPaths, *, max_evidence: int = 10, notebook_lines: int = 20) -> str:
    state = load_state(paths)
    gate = check_gate(paths)
    active = [item for item in state["requirements"] if item.get("status") != "superseded"]
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
    if not active:
        lines.append("- （暂无 active requirement）")
    for requirement in active:
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
