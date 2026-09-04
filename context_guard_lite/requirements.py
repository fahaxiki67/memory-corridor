from __future__ import annotations

from .contract import GuardError, ProjectPaths, append_event, append_notebook, load_state, save_state, utc_now

KINDS = {"must", "avoid", "acceptance"}
STATUSES = {"open", "blocked", "done", "superseded"}


def _clean_text(value: str) -> str:
    return " ".join(str(value).split())


def _next_id(items: list[dict], prefix: str) -> str:
    return f"{prefix}{len(items) + 1:03d}"


def get_requirement(state: dict, requirement_id: str) -> dict:
    for requirement in state["requirements"]:
        if requirement["id"].upper() == requirement_id.upper():
            return requirement
    raise GuardError(f"找不到 requirement: {requirement_id}")


def add_requirement(paths: ProjectPaths, text: str, kind: str = "must") -> dict:
    clean_text = _clean_text(text)
    if not clean_text:
        raise GuardError("requirement 内容不能为空")
    if kind not in KINDS:
        raise GuardError(f"不支持的 requirement 类型: {kind}，可选 {sorted(KINDS)}")
    state = load_state(paths)
    requirement_id = _next_id(state["requirements"], "R")
    requirement = {
        "id": requirement_id,
        "kind": kind,
        "text": clean_text,
        "status": "open",
        "revision": 1,
        "history": [],
        "created_at": utc_now(),
        "updated_at": utc_now(),
    }
    state["requirements"].append(requirement)
    save_state(paths, state)
    append_event(paths, "requirement.add", {"id": requirement_id, "kind": kind, "revision": 1})
    append_notebook(paths, f"新增 requirement {requirement_id}", [f"类型：{kind}", clean_text])
    return requirement


def import_requirements(paths: ProjectPaths, lines, kind: str = "must") -> dict:
    """批量导入 requirements：每行一条，空行与 # 注释行跳过。

    与逐条 add_requirement 的区别只在写入策略：state 只保存一次，
    避免大批量导入时的写放大；事件与旁记事本仍逐条记录，保持审计粒度。
    """
    if kind not in KINDS:
        raise GuardError(f"不支持的 requirement 类型: {kind}，可选 {sorted(KINDS)}")
    state = load_state(paths)
    created: list[dict] = []
    skipped = 0
    for raw_line in lines:
        clean_text = _clean_text(raw_line)
        if not clean_text or clean_text.startswith("#"):
            skipped += 1
            continue
        requirement = {
            "id": _next_id(state["requirements"], "R"),
            "kind": kind,
            "text": clean_text,
            "status": "open",
            "revision": 1,
            "history": [],
            "created_at": utc_now(),
            "updated_at": utc_now(),
        }
        state["requirements"].append(requirement)
        created.append(requirement)
    if created:
        save_state(paths, state)
        for requirement in created:
            append_event(
                paths,
                "requirement.add",
                {"id": requirement["id"], "kind": kind, "revision": 1, "imported": True},
            )
            append_notebook(paths, f"新增 requirement {requirement['id']}", [f"类型：{kind}", requirement["text"]])
    return {"imported": created, "skipped": skipped}


def mark_done(paths: ProjectPaths, requirement_id: str) -> tuple[dict, bool]:
    """把 requirement 标记为 done，并返回 (requirement, 是否已满足门禁证据条件)。

    门禁语义由 check_gate 定义，本函数只做同等判定用于提示，不产生任何 evidence。
    """
    requirement = update_requirement(paths, requirement_id, status="done")
    state = load_state(paths)
    matching = [
        item
        for item in state["evidence"]
        if item.get("requirement_id", "").upper() == requirement["id"].upper()
        and item.get("requirement_revision") == requirement.get("revision", 1)
    ]
    latest = matching[-1] if matching else None
    satisfied = latest is not None and latest.get("result") == "success"
    return requirement, satisfied


def update_requirement(
    paths: ProjectPaths,
    requirement_id: str,
    *,
    text: str | None = None,
    kind: str | None = None,
    status: str | None = None,
    reason: str | None = None,
) -> dict:
    state = load_state(paths)
    requirement = get_requirement(state, requirement_id)
    if text is None and kind is None and status is None:
        raise GuardError("至少提供 --text、--kind 或 --status 之一")
    if kind is not None and kind not in KINDS:
        raise GuardError(f"不支持的 requirement 类型: {kind}，可选 {sorted(KINDS)}")
    if status is not None and status not in STATUSES:
        raise GuardError(f"不支持的 requirement 状态: {status}，可选 {sorted(STATUSES)}")

    old_text = requirement["text"]
    old_kind = requirement["kind"]
    old_revision = requirement["revision"]
    new_text = _clean_text(text) if text is not None else old_text
    new_kind = kind or old_kind
    if not new_text:
        raise GuardError("requirement 内容不能为空")

    revision_changed = new_text != old_text or new_kind != old_kind
    if revision_changed:
        requirement["history"].append(
            {
                "revision": old_revision,
                "kind": old_kind,
                "text": old_text,
                "status": requirement["status"],
                "at": utc_now(),
            }
        )
        requirement["revision"] += 1
        requirement["text"] = new_text
        requirement["kind"] = new_kind
    if status is not None:
        requirement["status"] = status
    requirement["updated_at"] = utc_now()
    save_state(paths, state)
    append_event(
        paths,
        "requirement.update",
        {
            "id": requirement["id"],
            "revision": requirement["revision"],
            "revision_changed": revision_changed,
            "status": requirement["status"],
        },
    )
    details = [f"状态：{requirement['status']}", f"版本：v{requirement['revision']}"]
    if revision_changed:
        details.append("文本或类型已变化，旧 evidence 不再自动适用")
    if reason:
        details.append(f"原因：{_clean_text(reason)}")
    append_notebook(paths, f"更新 requirement {requirement['id']}", details + [requirement["text"]])
    return requirement

