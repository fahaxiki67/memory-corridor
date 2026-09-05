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
        if not state["requirements"]:
            # 账本为空是「信息不足」，不是「验收失败」：新用户装上 hook 后
            # 每回合都被 no_requirements 阻塞，会在理解工具之前先被惹恼。
            # 门禁从第一条 requirement 起才生效，空账本放行并给引导。
            return {
                "ok": True,
                "status": "idle",
                "summary": "账本为空，完成门禁尚未启用。记录第一条 requirement 后自动生效。",
                "blocking": [],
                "satisfied": [],
            }
        # 曾经有需求、现在全部 superseded：没有可验收目标，这是真异常，仍然阻塞。
        return {
            "ok": False,
            "status": "blocked",
            "summary": "所有 requirement 都已 superseded，没有可验收的目标。",
            "blocking": [{"reason": "all_superseded"}],
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

