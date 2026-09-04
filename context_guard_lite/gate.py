from __future__ import annotations

from .contract import ProjectPaths, load_state


def check_gate(paths: ProjectPaths) -> dict:
    state = load_state(paths)
    contract = state["contract"]
    if not contract.get("enabled", False):
        return {
            "ok": True,
            "status": "disabled",
            "summary": "保护已关闭，完成门禁不生效。",
            "blocking": [],
            "satisfied": [],
        }

    active = [item for item in state["requirements"] if item.get("status") != "superseded"]
    if not active:
        return {
            "ok": False,
            "status": "blocked",
            "summary": "没有 active requirements，不能宣称任务完成。",
            "blocking": [{"reason": "no_requirements"}],
            "satisfied": [],
        }

    blocking = []
    satisfied = []
    for requirement in active:
        matching = [
            item
            for item in state["evidence"]
            if item.get("requirement_id", "").upper() == requirement["id"].upper()
            and item.get("requirement_revision") == requirement.get("revision", 1)
        ]
        latest = matching[-1] if matching else None
        reasons = []
        if requirement.get("status") != "done":
            reasons.append(f"状态为 {requirement.get('status')}，不是 done")
        if latest is None:
            reasons.append("没有匹配当前版本的 evidence")
        elif latest.get("result") != "success":
            reasons.append(f"最新 evidence {latest.get('id')} 结果为 {latest.get('result')}")
        if reasons:
            blocking.append(
                {
                    "requirement_id": requirement["id"],
                    "text": requirement["text"],
                    "status": requirement.get("status"),
                    "revision": requirement.get("revision", 1),
                    "reasons": reasons,
                }
            )
        else:
            satisfied.append(
                {
                    "requirement_id": requirement["id"],
                    "evidence_id": latest["id"],
                    "revision": requirement.get("revision", 1),
                }
            )

    ok = not blocking
    return {
        "ok": ok,
        "status": "pass" if ok else "blocked",
        "summary": "所有 active requirements 均已 done，并有当前版本的 success evidence。"
        if ok
        else f"仍有 {len(blocking)} 个 requirement 未通过完成门禁。",
        "blocking": blocking,
        "satisfied": satisfied,
    }

