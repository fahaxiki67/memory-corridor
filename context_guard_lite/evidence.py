from __future__ import annotations

from .contract import GuardError, ProjectPaths, append_event, append_notebook, load_state, utc_now
from .locking import state_transaction
from .requirements import get_requirement

RESULTS = {"success", "failed", "unknown"}


def matching_evidence(state: dict, requirement: dict) -> list[dict]:
    """evidence ↔ requirement 的唯一绑定规则：ID 大小写不敏感 + revision 精确相等。

    门禁判定、恢复包、done 提示共用这一条检索规则，避免多处手写匹配条件后漂移。
    """
    requirement_id = str(requirement.get("id", "")).upper()
    revision = requirement.get("revision", 1)
    return [
        item
        for item in state["evidence"]
        if str(item.get("requirement_id", "")).upper() == requirement_id
        and item.get("requirement_revision") == revision
    ]


def latest_matching_evidence(state: dict, requirement: dict) -> dict | None:
    """绑定到 requirement 当前 revision 的最新一条 evidence；没有则 None。"""
    matching = matching_evidence(state, requirement)
    return matching[-1] if matching else None


def _clean(value: str | None) -> str | None:
    if value is None:
        return None
    value = " ".join(str(value).split())
    return value or None


def add_evidence(
    paths: ProjectPaths,
    requirement_id: str,
    summary: str,
    result: str,
    *,
    target: str | None = None,
    command: str | None = None,
) -> dict:
    clean_summary = _clean(summary)
    if not clean_summary:
        raise GuardError("evidence summary 不能为空")
    if result not in RESULTS:
        raise GuardError(f"不支持的 evidence 结果: {result}，可选 {sorted(RESULTS)}")
    with state_transaction(paths) as state:
        requirement = get_requirement(state, requirement_id)
        evidence_id = f"E{len(state['evidence']) + 1:03d}"
        evidence = {
            "id": evidence_id,
            "requirement_id": requirement["id"],
            "requirement_revision": requirement["revision"],
            "result": result,
            "summary": clean_summary,
            "target": _clean(target),
            "command": _clean(command),
            "created_at": utc_now(),
        }
        state["evidence"].append(evidence)
        requirement = dict(requirement)
    append_event(
        paths,
        "evidence.add",
        {
            "id": evidence_id,
            "requirement_id": requirement["id"],
            "requirement_revision": requirement["revision"],
            "result": result,
        },
    )
    details = [
        f"绑定：{requirement['id']} v{requirement['revision']}",
        f"结果：{result}",
        f"摘要：{clean_summary}",
    ]
    if target:
        details.append(f"目标：{_clean(target)}")
    if command:
        details.append(f"命令：{_clean(command)}")
    append_notebook(paths, f"新增 evidence {evidence_id}", details)
    return evidence


def list_evidence(paths: ProjectPaths) -> list[dict]:
    return load_state(paths)["evidence"]

