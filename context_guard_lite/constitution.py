"""项目章程（constitution）：项目级不可违反约束清单。

借鉴 spec-kit 等社区关于项目 constitution 的实践：把「整个项目期间都不应
违反的约束」放在一份人可直接编辑的 Markdown 文件
（``.context-guard/constitution.md``，一行一条约束），随恢复包、``status``
输出与 Stop hook 阻塞清单展示。

与 requirement 的分工：requirement 会完成、会被 superseded，章程在整个项目
生命周期持续生效，且不参与门禁判定。

边界（对齐项目哲学「只展示不判定」「缺失≠0」「不静默」）：

- 工具不判断某个行为是否违反章程；违反与否由人/AI 自行判断，工具不猜；
- 章程不写入 ``state.json``（无 schema 变更与迁移），「有无章程」在每次
  输出时现场检查文件存在性——「未设置」与「设置了但 0 条」显式区分；
- 读取失败不得静默：渲染区块时显示占位说明，绝不冒充「无章程」，也绝不
  影响门禁判定（章程是辅助信息，不是门禁输入）。
"""

from __future__ import annotations

from pathlib import Path

from .contract import CONSTITUTION_FILE_NAME, GuardError, ProjectPaths, append_event, atomic_write

RELATIVE_PATH = f".context-guard/{CONSTITUTION_FILE_NAME}"
_HEADER = "# 项目章程（Constitution）"
_HINT_COMMENT = "# 一行一条约束；空行与注释行不计入。本文件可直接手工编辑。"
_SECTION_HEADER = "章程约束"
_DISPLAY_ONLY_NOTE = "（只展示不判定：章程是否被违反由人/AI 自行判断，工具不猜。文件：{path}）"


def constitution_path(paths: ProjectPaths) -> Path:
    return paths.constitution


def _clean_text(value: str) -> str:
    return " ".join(str(value).split())


def _is_comment(line: str) -> bool:
    # # Markdown 标题/注释与 <!-- HTML 注释 --> 都不计入约束。
    return line.startswith("#") or line.startswith("<!--")


def _parse_constraints(raw_lines: list[str]) -> list[str]:
    """有效约束行 = 非空且非注释行（与 requirements import 的跳过规则同源）。"""
    constraints: list[str] = []
    for line in raw_lines:
        clean = line.strip()
        if not clean or _is_comment(clean):
            continue
        constraints.append(clean)
    return constraints


def _read_raw_lines(path: Path) -> list[str]:
    # utf-8-sig：容忍 Windows 记事本留下的 BOM（人可直接编辑的文件必须有此容忍）。
    try:
        return path.read_text(encoding="utf-8-sig", errors="replace").splitlines()
    except OSError as exc:
        raise GuardError(f"无法读取章程文件 {path}: {exc}") from exc


def load_constraints(paths: ProjectPaths) -> list[str]:
    """读取全部约束行；未设置返回 []，读取失败抛 GuardError（显式操作路径用）。"""
    path = constitution_path(paths)
    if not path.exists():
        return []
    return _parse_constraints(_read_raw_lines(path))


def add_constraint(paths: ProjectPaths, text: str) -> dict:
    """追加一条约束；同一约束已存在时不重复写入（幂等）。

    返回 ``{"status": "added"|"exists", "text": ..., "count": N}``。
    写入采用同目录临时文件加替换的原子写（对齐 state.json 的写法），且保留
    文件里人手工添加的注释与其他内容——只追加，不重排。
    """
    clean = _clean_text(text)
    if not clean:
        raise GuardError("章程约束不能为空")
    path = constitution_path(paths)
    if not path.exists():
        content = f"{_HEADER}\n{_HINT_COMMENT}\n\n{clean}\n"
        atomic_write(path, content)
        append_event(paths, "constitution.set", {"text": clean, "count": 1})
        return {"status": "added", "text": clean, "count": 1}
    raw_lines = _read_raw_lines(path)
    existing = _parse_constraints(raw_lines)
    if clean in existing:
        return {"status": "exists", "text": clean, "count": len(existing)}
    body = "\n".join(raw_lines)
    if body and not body.endswith("\n"):
        body += "\n"
    atomic_write(path, body + clean + "\n")
    count = len(existing) + 1
    append_event(paths, "constitution.set", {"text": clean, "count": count})
    return {"status": "added", "text": clean, "count": count}


def clear_constitution(paths: ProjectPaths) -> bool:
    """删除章程文件；未设置时返回 False（幂等，不报错、不写事件）。"""
    path = constitution_path(paths)
    if not path.exists():
        return False
    try:
        path.unlink()
    except OSError as exc:
        raise GuardError(f"无法删除章程文件 {path}: {exc}") from exc
    append_event(paths, "constitution.clear", {})
    return True


def render_section(paths: ProjectPaths) -> list[str]:
    """「章程约束」区块的单一渲染实现；未设置章程返回 []（不出现区块）。

    恢复包、status 输出与 Stop hook 阻塞清单共用本函数，保证各处内容一致。
    文件存在但读取失败时显示占位说明而非静默跳过；本函数不抛异常，
    保证章程（辅助信息）永远不会破坏门禁与恢复包链路。
    """
    path = constitution_path(paths)
    if not path.exists():
        return []
    try:
        constraints = load_constraints(paths)
    except GuardError as exc:
        return [
            f"## {_SECTION_HEADER}（读取失败）",
            "",
            f"- （无法读取章程文件：{exc}；请人工检查 {RELATIVE_PATH}，本区块只展示不判定）",
        ]
    if not constraints:
        return [
            f"## {_SECTION_HEADER}（0 条）",
            "",
            f"- （章程文件存在但没有有效约束行：空行与注释行不计入。文件：{RELATIVE_PATH}）",
        ]
    lines = [f"## {_SECTION_HEADER}（{len(constraints)} 条）", ""]
    lines.extend(f"- {item}" for item in constraints)
    lines.append("")
    lines.append(_DISPLAY_ONLY_NOTE.format(path=RELATIVE_PATH))
    return lines


def constitution_summary(paths: ProjectPaths) -> dict | None:
    """``status --json`` 用的宽松摘要；未设置返回 None（缺失≠0，显式区分）。"""
    path = constitution_path(paths)
    if not path.exists():
        return None
    try:
        constraints = load_constraints(paths)
    except (GuardError, OSError) as exc:
        return {"error": str(exc)}
    return {"count": len(constraints), "constraints": constraints}
