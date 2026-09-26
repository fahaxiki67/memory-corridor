from __future__ import annotations

import json
import os
import sys
import tempfile
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

APP_DIR_NAME = ".context-guard"
SCHEMA_VERSION = 1
# 章程文件（constitution 模块消费）；不是 state 的一部分，不参与 schema 与迁移。
CONSTITUTION_FILE_NAME = "constitution.md"
GITIGNORE_NAME = ".gitignore"
DATA_IGNORE_ENTRY = ".context-guard/"
_GITIGNORE_NOTE = "# The local ledger can contain task text and should not be committed by accident."
# 事件暂存（outbox）：events.jsonl 暂时不可写时事件落这里，下一条命令幂等补记。
PENDING_EVENTS_NAME = "events.pending.jsonl"

# schema v1 的受控词表（唯一权威定义处）。requirements/evidence 模块从这里
# re-export 既有名称，业务校验与 load_state 的结构校验共用同一份，避免多处
# 手写集合后漂移。
REQUIREMENT_KINDS = frozenset({"must", "avoid", "acceptance"})
REQUIREMENT_STATUSES = frozenset({"open", "blocked", "done", "superseded"})
EVIDENCE_RESULTS = frozenset({"success", "failed", "unknown"})


class GuardError(RuntimeError):
    """A user-fixable Context Guard error."""


@dataclass(frozen=True)
class ProjectPaths:
    root: Path
    data: Path
    state: Path
    events: Path
    notebook: Path
    recovery: Path
    constitution: Path


def project_paths(root: Path | str | None = None) -> ProjectPaths:
    root_path = (Path.cwd() if root is None else Path(root)).expanduser().resolve()
    data = root_path / APP_DIR_NAME
    return ProjectPaths(
        root=root_path,
        data=data,
        state=data / "state.json",
        events=data / "events.jsonl",
        notebook=data / "notebook.md",
        recovery=data / "recovery.md",
        constitution=data / CONSTITUTION_FILE_NAME,
    )


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _one_line(value: str) -> str:
    return " ".join(str(value).split())


def atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", newline="\n", dir=path.parent, prefix=f".{path.name}.", suffix=".tmp", delete=False
        ) as handle:
            temporary = Path(handle.name)
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary and temporary.exists():
            temporary.unlink()


def _atomic_write_json(path: Path, value: dict) -> None:
    atomic_write(path, json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def _ensure_initialized(paths: ProjectPaths) -> None:
    if not paths.state.exists():
        raise GuardError(f"当前目录未初始化，请先运行: memory-corridor init\n目录: {paths.root}")


def ensure_gitignore_ignores_data(root: Path | str) -> str:
    """确保项目 .gitignore 忽略 .context-guard/，避免任务文本被误提交。

    只增不删、幂等：已忽略则原样保留（返回 already-ignored）；有 .gitignore 但
    缺条目时在末尾追加（updated）；没有 .gitignore 时创建最小文件（created）。
    任何写入失败都不阻塞 init，返回 "skipped: <原因>" 由调用方提示。
    """
    gitignore = Path(root) / GITIGNORE_NAME
    ignored_forms = {DATA_IGNORE_ENTRY, DATA_IGNORE_ENTRY.rstrip("/")}
    try:
        if gitignore.exists():
            existing = gitignore.read_text(encoding="utf-8", errors="replace")
            if any(line.strip() in ignored_forms for line in existing.splitlines()):
                return "already-ignored"
            prefix = "" if (not existing or existing.endswith("\n")) else "\n"
            with gitignore.open("a", encoding="utf-8", newline="\n") as handle:
                handle.write(f"{prefix}\n{_GITIGNORE_NOTE}\n{DATA_IGNORE_ENTRY}\n")
            return "updated"
        gitignore.write_text(f"{_GITIGNORE_NOTE}\n{DATA_IGNORE_ENTRY}\n", encoding="utf-8")
        return "created"
    except OSError as exc:
        return f"skipped: {exc}"


def init_project(root: Path | str | None = None, name: str | None = None, outcome: dict | None = None) -> dict:
    paths = project_paths(root)
    if not paths.root.exists() or not paths.root.is_dir():
        raise GuardError(f"项目目录不存在: {paths.root}")
    if paths.data.exists():
        raise GuardError(f"项目已经存在 Context Guard 数据，请勿覆盖: {paths.data}")

    created_at = utc_now()
    project_name = _one_line(name or paths.root.name) or "未命名项目"
    state = {
        "schema_version": SCHEMA_VERSION,
        "project": {"name": project_name, "root": str(paths.root)},
        "contract": {
            "enabled": True,
            "created_at": created_at,
            "updated_at": created_at,
        },
        "requirements": [],
        "evidence": [],
        "notes": [],
        "created_at": created_at,
        "updated_at": created_at,
    }
    paths.data.mkdir(parents=True)
    _atomic_write_json(paths.state, state)
    atomic_write(paths.notebook, f"# 记忆回廊（Context Guard Lite 2.0）Notebook\n\n项目：{project_name}\n\n")
    # 隐私边界：账本含任务文本，默认必须被 git 忽略（README 承诺的行为）。
    # 失败不阻塞初始化，结果进事件日志与 CLI 提示（outcome 出参模式同 read_events.malformed）。
    gitignore_result = ensure_gitignore_ignores_data(paths.root)
    if outcome is not None:
        outcome["gitignore"] = gitignore_result
    append_event(paths, "contract.init", {"project": project_name, "gitignore": gitignore_result})
    append_notebook(paths, "初始化", [f"项目：{project_name}", "保护状态：开启"])
    return state


def _is_filled_text(value: object) -> bool:
    """非空字符串（容忍纯空白以外的任何内容；空白字符串视为缺失）。"""
    return isinstance(value, str) and bool(value.strip())


def _is_count(value: object) -> bool:
    """真正的整数。bool 是 int 的子类，True/False 不能冒充 revision。"""
    return isinstance(value, int) and not isinstance(value, bool)


def _corrupt(paths: ProjectPaths, detail: str) -> GuardError:
    return GuardError(
        f"状态文件损坏（{detail}），已拒绝读取且未修改原文件。"
        f"请人工检查或从备份恢复 {paths.state}；确认无误前完成门禁按阻塞处理。"
    )


def validate_state(state: dict, paths: ProjectPaths) -> None:
    """严格校验 schema v1 状态结构；损坏抛 GuardError，不修改任何文件。

    为什么两侧都拦：``load_state`` 侧防止坏数据被当正常状态放行（预审实证：
    ``contract`` 缺 ``enabled`` 时门禁把它当 disabled 直接 ``ok=True``，损坏
    状态反而「通过」）；``save_state`` 侧防止程序自身把不完整状态写回磁盘。
    这里刻意不提供宽松默认值：兼容旧数据必须走显式迁移（升 schema_version），
    而不是静默补字段掩盖坏数据。用户明确 ``enabled=false`` 与空账本 idle 的
    既有设计不受影响——校验只拒绝「缺字段/类型错/结构坏」，不改变语义。
    """
    project = state["project"]
    if not _is_filled_text(project.get("name")):
        raise _corrupt(paths, "project.name 缺失或不是非空字符串")
    if not _is_filled_text(project.get("root")):
        raise _corrupt(paths, "project.root 缺失或不是非空字符串")
    for key in ("created_at", "updated_at"):
        if not _is_filled_text(state.get(key)):
            raise _corrupt(paths, f"{key} 缺失或不是非空字符串")

    contract = state["contract"]
    enabled = contract.get("enabled")
    if type(enabled) is not bool:
        raise _corrupt(
            paths,
            f"contract.enabled 缺失或不是真正的布尔值（实际 {enabled!r}）；"
            "如需关闭保护请运行 memory-corridor off，不要手工删除字段",
        )
    for key in ("created_at", "updated_at"):
        if not _is_filled_text(contract.get(key)):
            raise _corrupt(paths, f"contract.{key} 缺失或不是非空字符串")
    if "strict" in contract and type(contract["strict"]) is not bool:
        # strict 是 v2.12 引入的可选开关（缺席=关闭）；一旦出现就必须是真正 bool，
        # 不允许 "on"/1 之类手工值混进来——那会让门禁语义变得不可预测。
        raise _corrupt(
            paths,
            f"contract.strict 不是真正的布尔值（实际 {contract['strict']!r}）；"
            "如需开关 strict 验收请运行 memory-corridor strict on/off",
        )

    requirement_by_id: dict[str, dict] = {}
    seen_requirement_ids: set[str] = set()
    for index, requirement in enumerate(state["requirements"]):
        label = f"requirements[{index}]"
        if not isinstance(requirement, dict):
            raise _corrupt(paths, f"{label} 不是对象")
        requirement_id = requirement.get("id")
        if not _is_filled_text(requirement_id):
            raise _corrupt(paths, f"{label}.id 缺失或不是非空字符串")
        id_key = requirement_id.upper()
        if id_key in seen_requirement_ids:
            raise _corrupt(paths, f"{label}.id 与其他 requirement 重复（ID 比较大小写不敏感）: {requirement_id}")
        seen_requirement_ids.add(id_key)
        if requirement.get("kind") not in REQUIREMENT_KINDS:
            raise _corrupt(paths, f"{label}.kind 非法（{requirement.get('kind')!r}，可选 {sorted(REQUIREMENT_KINDS)}）")
        if not _is_filled_text(requirement.get("text")):
            raise _corrupt(paths, f"{label}.text 缺失或不是非空字符串")
        if requirement.get("status") not in REQUIREMENT_STATUSES:
            raise _corrupt(paths, f"{label}.status 非法（{requirement.get('status')!r}，可选 {sorted(REQUIREMENT_STATUSES)}）")
        revision = requirement.get("revision")
        if not _is_count(revision) or revision < 1:
            raise _corrupt(paths, f"{label}.revision 必须是 >=1 的整数（实际 {revision!r}）")
        if not isinstance(requirement.get("history"), list):
            raise _corrupt(paths, f"{label}.history 不是列表")
        for key in ("created_at", "updated_at"):
            if not _is_filled_text(requirement.get(key)):
                raise _corrupt(paths, f"{label}.{key} 缺失或不是非空字符串")
        requirement_by_id[id_key] = requirement

    seen_evidence_ids: set[str] = set()
    for index, evidence in enumerate(state["evidence"]):
        label = f"evidence[{index}]"
        if not isinstance(evidence, dict):
            raise _corrupt(paths, f"{label} 不是对象")
        evidence_id = evidence.get("id")
        if not _is_filled_text(evidence_id):
            raise _corrupt(paths, f"{label}.id 缺失或不是非空字符串")
        id_key = evidence_id.upper()
        if id_key in seen_evidence_ids:
            raise _corrupt(paths, f"{label}.id 与其他 evidence 重复（ID 比较大小写不敏感）: {evidence_id}")
        seen_evidence_ids.add(id_key)
        requirement_id = evidence.get("requirement_id")
        if not _is_filled_text(requirement_id):
            raise _corrupt(paths, f"{label}.requirement_id 缺失或不是非空字符串")
        bound = requirement_by_id.get(requirement_id.upper())
        if bound is None:
            raise _corrupt(paths, f"{label}.requirement_id={requirement_id} 没有对应的 requirement（证据绑定断裂）")
        evidence_revision = evidence.get("requirement_revision")
        if not _is_count(evidence_revision) or evidence_revision < 1:
            raise _corrupt(paths, f"{label}.requirement_revision 必须是 >=1 的整数（实际 {evidence_revision!r}）")
        if evidence_revision > bound["revision"]:
            raise _corrupt(
                paths,
                f"{label} 绑定 v{evidence_revision}，但 requirement {bound['id']} 当前只有 v{bound['revision']}"
                "（证据不能绑定未来版本）",
            )
        if evidence.get("result") not in EVIDENCE_RESULTS:
            raise _corrupt(paths, f"{label}.result 非法（{evidence.get('result')!r}，可选 {sorted(EVIDENCE_RESULTS)}）")
        if not _is_filled_text(evidence.get("summary")):
            raise _corrupt(paths, f"{label}.summary 缺失或不是非空字符串")
        if not _is_filled_text(evidence.get("created_at")):
            raise _corrupt(paths, f"{label}.created_at 缺失或不是非空字符串")

    seen_note_ids: set[str] = set()
    for index, note in enumerate(state["notes"]):
        label = f"notes[{index}]"
        if not isinstance(note, dict):
            raise _corrupt(paths, f"{label} 不是对象")
        note_id = note.get("id")
        if not _is_filled_text(note_id):
            raise _corrupt(paths, f"{label}.id 缺失或不是非空字符串")
        id_key = note_id.upper()
        if id_key in seen_note_ids:
            raise _corrupt(paths, f"{label}.id 与其他笔记重复: {note_id}")
        seen_note_ids.add(id_key)
        for key in ("kind", "source", "text", "created_at"):
            if not _is_filled_text(note.get(key)):
                raise _corrupt(paths, f"{label}.{key} 缺失或不是非空字符串")


def load_state(paths: ProjectPaths) -> dict:
    _ensure_initialized(paths)
    try:
        state = json.loads(paths.state.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise GuardError(f"无法读取状态文件，请保留现场后检查 {paths.state}: {exc}") from exc
    if not isinstance(state, dict):
        raise GuardError(f"状态文件不是 JSON 对象: {paths.state}")
    version = state.get("schema_version")
    if version != SCHEMA_VERSION:
        if isinstance(version, int) and version > SCHEMA_VERSION:
            # 「数据更新、工具旧」不是「数据损坏」：给出可执行的出路而不是含糊报错。
            raise GuardError(
                f"状态文件由更新版本的 memory-corridor 写入（schema v{version} > 本工具 v{SCHEMA_VERSION}），"
                f"请先升级本工具再操作，不要手工改写: {paths.state}"
            )
        raise GuardError(f"不支持的状态版本或状态损坏: {paths.state}")
    for key in ("project", "contract", "requirements", "evidence", "notes"):
        if key not in state:
            raise GuardError(f"状态文件缺少字段 {key}: {paths.state}")
    if not isinstance(state["project"], dict) or not isinstance(state["contract"], dict):
        raise GuardError(f"状态文件中的 project/contract 不是对象: {paths.state}")
    if not all(isinstance(state[key], list) for key in ("requirements", "evidence", "notes")):
        raise GuardError(f"状态文件中的 requirements/evidence/notes 不是列表: {paths.state}")
    validate_state(state, paths)
    return state


def save_state(paths: ProjectPaths, state: dict) -> None:
    # 写入前同样严格校验：防止程序自身把不完整/损坏的状态落盘，让「磁盘上的
    # state.json 一定通过 load_state 校验」成为不变量。校验失败时原子写尚未
    # 发生，原文件保持修改前内容。
    validate_state(state, paths)
    state["updated_at"] = utc_now()
    state["contract"]["updated_at"] = state["updated_at"]
    _atomic_write_json(paths.state, state)


def append_event(paths: ProjectPaths, event_type: str, details: dict | None = None) -> str:
    """追加一条事件到 events.jsonl；返回 "written" 或 "pending"（已暂存待重放）。

    真源与提交语义：state.json 是唯一业务真源，``state_transaction`` 的原子
    替换是提交点；events.jsonl/notebook.md 是提交后的审计与人读衍生记录。
    历史缺陷是「状态已提交、随后审计写入失败」时命令整体报错，用户无法区分
    「命令失败」和「状态已变」。现在：每条事件带唯一幂等 id；events.jsonl
    写入失败（权限/磁盘/替换窗口）时事件原样暂存到 events.pending.jsonl，
    由下一条命令开头/结尾的 ``flush_pending_events`` 锁内幂等补记（按 id 去重，
    崩溃在「已写入、pending 未清理」之间也不会产生重复行）。暂存也失败时抛出
    OSError——绝不吞掉错误假装成功，由 CLI 提示用户核对状态。
    """
    paths.data.mkdir(parents=True, exist_ok=True)
    event = {"at": utc_now(), "type": event_type, "id": uuid.uuid4().hex[:12], **(details or {})}
    line = json.dumps(event, ensure_ascii=False)
    try:
        with paths.events.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(line + "\n")
        return "written"
    except OSError as events_exc:
        pending_path = paths.data / PENDING_EVENTS_NAME
        try:
            with pending_path.open("a", encoding="utf-8", newline="\n") as handle:
                handle.write(line + "\n")
        except OSError as pending_exc:
            raise OSError(
                f"事件无法写入 {paths.events}（{events_exc}），暂存到 {pending_path} 也失败（{pending_exc}）；"
                "注意：状态文件的修改已经提交、不受影响，请检查磁盘与权限后重试"
            ) from pending_exc
        return "pending"


def flush_pending_events(paths: ProjectPaths) -> tuple[int, int]:
    """把 events.pending.jsonl 暂存的事件幂等补记进 events.jsonl（锁内执行）。

    返回 ``(成功补记条数, 仍无法写入条数)``。无法解析的暂存行（中断截断）
    丢弃并告警；同一事件 id 已在 events.jsonl 中的跳过。补记成功但 pending
    清理失败时不必处理：下次重放按 id 去重，天然幂等。
    """
    from .locking import state_lock

    pending_path = paths.data / PENDING_EVENTS_NAME
    if not paths.state.exists() or not pending_path.exists():
        return 0, 0
    try:
        raw_lines = pending_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError as exc:
        raise GuardError(f"无法读取暂存事件文件 {pending_path}: {exc}") from exc
    lines: list[str] = []
    malformed = 0
    for raw in raw_lines:
        stripped = raw.strip()
        if not stripped:
            continue
        try:
            value = json.loads(stripped)
        except json.JSONDecodeError:
            malformed += 1
            continue
        if not isinstance(value, dict):
            malformed += 1
            continue
        lines.append(stripped)
    if malformed:
        print(
            f"警告：{pending_path} 有 {malformed} 行无法解析（可能被中断截断），重放时已丢弃。",
            file=sys.stderr,
        )
    replayed = 0
    remaining: list[str] = []
    with state_lock(paths):
        # 去重基线必须在锁内读取：并发的另一个进程可能刚补记完同一批事件，
        # 锁外读到的集合是陈旧的，会造成重复行（故障注入实测）。
        try:
            existing_ids = {event.get("id") for event in read_events(paths) if event.get("id")}
        except GuardError:
            # events.jsonl 暂时不可读（缺失/权限，正是事件被暂存的常见原因）：
            # 以空基线尽力补记，append 会重建文件；total 去重由下次重放兜底。
            existing_ids = set()
        for line in lines:
            try:
                event_id = json.loads(line).get("id")
            except json.JSONDecodeError:  # pragma: no cover - 上面已过滤，防御性保留
                continue
            if event_id and event_id in existing_ids:
                continue
            try:
                with paths.events.open("a", encoding="utf-8", newline="\n") as handle:
                    handle.write(line + "\n")
            except OSError:
                remaining.append(line)
                continue
            if event_id:
                existing_ids.add(event_id)
            replayed += 1
        try:
            if remaining:
                atomic_write(pending_path, "\n".join(remaining) + "\n")
            else:
                pending_path.unlink()
        except OSError:
            # 清理失败可自愈：pending 里多留的行会在下次重放时按 id 去重跳过。
            pass
    return replayed, len(remaining)


def append_notebook(paths: ProjectPaths, title: str, lines: list[str]) -> bool:
    """追加旁记事本；返回是否写入成功。

    notebook 是给人读的衍生记录（内容可由 state.json/events 重建，纪律明确
    不强求多文件原子）。写入失败只向 stderr 告警并返回 False，不抛异常：
    状态与事件（真源和审计）已提交的事实，不应因衍生文件暂时不可写而被
    「命令失败」的假象掩盖。
    """
    paths.data.mkdir(parents=True, exist_ok=True)
    try:
        if not paths.notebook.exists():
            atomic_write(paths.notebook, "# 记忆回廊（Context Guard Lite 2.0）Notebook\n\n")
        with paths.notebook.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(f"## {utc_now()} — {_one_line(title)}\n")
            for line in lines:
                handle.write(f"- {_one_line(line)}\n")
            handle.write("\n")
        return True
    except OSError as exc:
        print(f"警告：旁记事本写入失败（{exc}）；记录仍在 state.json 与 events.jsonl 中，notebook 可稍后重建。", file=sys.stderr)
        return False


def set_enabled(paths: ProjectPaths, enabled: bool) -> dict:
    from .locking import state_transaction

    with state_transaction(paths) as state:
        state["contract"]["enabled"] = enabled
    label = "开启" if enabled else "关闭"
    append_event(paths, "contract.on" if enabled else "contract.off", {"enabled": enabled})
    append_notebook(paths, f"保护{label}", [f"保护状态：{label}"])
    return state


def set_strict(paths: ProjectPaths, strict: bool) -> dict:
    """开关 strict 验收：开启后完成门禁只认「实际执行且未过期」的 success evidence。

    与 set_enabled 同构：锁内改 contract、锁外记事件与旁记事本。
    """
    from .locking import state_transaction

    with state_transaction(paths) as state:
        state["contract"]["strict"] = strict
    label = "开启" if strict else "关闭"
    append_event(paths, "contract.strict_on" if strict else "contract.strict_off", {"strict": strict})
    append_notebook(paths, f"strict 验收{label}", [f"strict 验收：{label}（success evidence 必须来自实际执行）"])
    return state


def add_note(paths: ProjectPaths, text: str, kind: str = "experience", source: str = "manual") -> dict:
    clean_text = _one_line(text)
    if not clean_text:
        raise GuardError("笔记内容不能为空")
    from .locking import state_transaction

    with state_transaction(paths) as state:
        note_id = f"N{len(state['notes']) + 1:03d}"
        note = {
            "id": note_id,
            "kind": kind,
            "source": _one_line(source) or "manual",
            "text": clean_text,
            "created_at": utc_now(),
        }
        state["notes"].append(note)
    append_event(paths, "note.add", {"id": note_id, "kind": kind, "source": note["source"]})
    append_notebook(paths, f"笔记 {note_id} [{kind}]", [f"来源：{note['source']}", clean_text])
    return note


def notebook_tail(paths: ProjectPaths, limit: int = 20) -> list[str]:
    if limit <= 0 or not paths.notebook.exists():
        return []
    try:
        lines = paths.notebook.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError as exc:
        raise GuardError(f"无法读取旁记事本: {exc}") from exc
    return lines[-limit:]


def read_events(
    paths: ProjectPaths,
    *,
    limit: int | None = None,
    event_type: str | None = None,
    malformed: dict | None = None,
) -> list[dict]:
    """只读读取 events.jsonl；跳过无法解析的行，不修改文件。

    ``malformed`` 传入 dict 时回填 ``{"count": N}``（N=被跳过的无法解析行数），
    供调用方向用户显式告警：审计日志损坏不应与空日志不可区分。
    """
    _ensure_initialized(paths)
    try:
        raw_lines = paths.events.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError as exc:
        raise GuardError(f"无法读取事件日志: {exc}") from exc
    events: list[dict] = []
    skipped = 0
    for line in raw_lines:
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            skipped += 1
            continue
        if not isinstance(event, dict):
            skipped += 1
            continue
        if event_type is not None and event.get("type") != event_type:
            continue
        events.append(event)
    if malformed is not None:
        malformed["count"] = skipped
    if limit is not None:
        events = events[-limit:] if limit > 0 else []
    return events
