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

# ZCode 客户端在 SessionStart 注入处按 24000 字符截断（Tdi=24e3，2026-09-12 客户端
# 源码核对：超限内容被 slice(0,24000)+"..." 静默截尾）。恢复包自限 20000 字符留出
# 余量：条目数有界但单条文本（requirement/evidence/note）无上限，极端账本下仍可能
# 超限，客户端静默截尾会丢掉结尾的 Completion Gate 指引，因此必须自己降级。
PACKET_CHAR_LIMIT = 20000
_NOTEBOOK_HEADER = "## 旁记事本最近记录"
_COMPLETED_HEADER = "## 已完成"
_EVIDENCE_HEADER = "## 最近 evidence"
_GATE_HEADER = "## Completion Gate"


def _split_sections(lines: list[str]) -> list[list[str]]:
    """按 '## ' 标题把行列表切成段（首段含包头，无标题行时归首段）。"""
    sections: list[list[str]] = []
    for line in lines:
        if line.startswith("## ") or not sections:
            sections.append([line])
        else:
            sections[-1].append(line)
    return sections


def _find_section(sections: list[list[str]], header: str) -> int:
    for index, section in enumerate(sections):
        if section and section[0].startswith(header):
            return index
    return -1


def _truncate_long_lines(lines: list[str], width: int = 200) -> list[str]:
    return [
        line if len(line) <= width else line[:width] + "…"
        for line in lines
    ]


def _fit_packet(text: str, limit: int = PACKET_CHAR_LIMIT) -> str:
    """把恢复包压进客户端注入限制内；降级顺序按信息价值从低到高牺牲。

    1. 整体超限 → 旁记事本段替换为占位行（人读的 notebook.md 仍在项目里）；
    2. 仍超限 → 「已完成」段替换为占位行（详情在 state.json）；
    3. 仍超限 → 「最近 evidence」只留前 3 条；
    4. 仍超限 → 超长行截到 200 字符（requirement/evidence 的超长文本）；
    5. 兜底 → 保留包头与 Completion Gate 段，中间替换为省略说明。
    """
    if len(text) <= limit:
        return text
    sections = _split_sections(text.rstrip("\n").split("\n"))
    notebook_index = _find_section(sections, _NOTEBOOK_HEADER)
    if notebook_index >= 0:
        sections[notebook_index] = [
            _NOTEBOOK_HEADER,
            "",
            "- （旁记事本超出注入上限已省略；全文见 .context-guard/notebook.md）",
        ]
    if sum(len(line) + 1 for section in sections for line in section) <= limit:
        return "\n".join(line for section in sections for line in section) + "\n"
    completed_index = _find_section(sections, _COMPLETED_HEADER)
    if completed_index >= 0:
        sections[completed_index] = [
            _COMPLETED_HEADER,
            "",
            "- （已完成 requirement 超出注入上限已省略；详情见 .context-guard/state.json）",
        ]
    evidence_index = _find_section(sections, _EVIDENCE_HEADER)
    if evidence_index >= 0:
        kept = sections[evidence_index][:6]  # 标题 + 空行 + 前 3 条 + 空行余量
        kept.append("- （更多 evidence 已省略；详情见 .context-guard/state.json）")
        sections[evidence_index] = kept
    joined = "\n".join(line for section in sections for line in section)
    if len(joined) <= limit:
        return joined + "\n"
    sections = [_truncate_long_lines(section) for section in sections]
    joined = "\n".join(line for section in sections for line in section)
    if len(joined) <= limit:
        return joined + "\n"
    gate_index = _find_section(sections, _GATE_HEADER)
    gate_lines = sections[gate_index] if gate_index >= 0 else []
    head_lines = [line for section in sections[:2] for line in section]
    budget = limit - len(gate_lines) - 2
    kept_head: list[str] = []
    for line in head_lines:
        if sum(len(item) + 1 for item in kept_head) + len(line) + 1 > budget:
            break
        kept_head.append(line)
    omitted = "- …（中间内容超出注入上限已省略；完整状态见 .context-guard/state.json）"
    return "\n".join(kept_head + [omitted] + gate_lines) + "\n"


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
    return _fit_packet("\n".join(lines) + "\n")


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
