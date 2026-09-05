from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

APP_DIR_NAME = ".context-guard"
SCHEMA_VERSION = 1


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


def init_project(root: Path | str | None = None, name: str | None = None) -> dict:
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
    append_event(paths, "contract.init", {"project": project_name})
    append_notebook(paths, "初始化", [f"项目：{project_name}", "保护状态：开启"])
    return state


def load_state(paths: ProjectPaths) -> dict:
    _ensure_initialized(paths)
    try:
        state = json.loads(paths.state.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise GuardError(f"无法读取状态文件，请保留现场后检查 {paths.state}: {exc}") from exc
    if not isinstance(state, dict) or state.get("schema_version") != SCHEMA_VERSION:
        raise GuardError(f"不支持的状态版本或状态损坏: {paths.state}")
    for key in ("project", "contract", "requirements", "evidence", "notes"):
        if key not in state:
            raise GuardError(f"状态文件缺少字段 {key}: {paths.state}")
    if not isinstance(state["project"], dict) or not isinstance(state["contract"], dict):
        raise GuardError(f"状态文件中的 project/contract 不是对象: {paths.state}")
    if not all(isinstance(state[key], list) for key in ("requirements", "evidence", "notes")):
        raise GuardError(f"状态文件中的 requirements/evidence/notes 不是列表: {paths.state}")
    return state


def save_state(paths: ProjectPaths, state: dict) -> None:
    state["updated_at"] = utc_now()
    state["contract"]["updated_at"] = state["updated_at"]
    _atomic_write_json(paths.state, state)


def append_event(paths: ProjectPaths, event_type: str, details: dict | None = None) -> None:
    paths.data.mkdir(parents=True, exist_ok=True)
    event = {"at": utc_now(), "type": event_type, **(details or {})}
    # events.jsonl 保持追加式；只有在实测体积增长确有必要时才加轮转。
    with paths.events.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(event, ensure_ascii=False) + "\n")


def append_notebook(paths: ProjectPaths, title: str, lines: list[str]) -> None:
    paths.data.mkdir(parents=True, exist_ok=True)
    if not paths.notebook.exists():
        atomic_write(paths.notebook, "# 记忆回廊（Context Guard Lite 2.0）Notebook\n\n")
    with paths.notebook.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(f"## {utc_now()} — {_one_line(title)}\n")
        for line in lines:
            handle.write(f"- {_one_line(line)}\n")
        handle.write("\n")


def set_enabled(paths: ProjectPaths, enabled: bool) -> dict:
    from .locking import state_transaction

    with state_transaction(paths) as state:
        state["contract"]["enabled"] = enabled
    label = "开启" if enabled else "关闭"
    append_event(paths, "contract.on" if enabled else "contract.off", {"enabled": enabled})
    append_notebook(paths, f"保护{label}", [f"保护状态：{label}"])
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


def read_events(paths: ProjectPaths, *, limit: int | None = None, event_type: str | None = None) -> list[dict]:
    """只读读取 events.jsonl；跳过无法解析的行，不修改文件。"""
    _ensure_initialized(paths)
    try:
        raw_lines = paths.events.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError as exc:
        raise GuardError(f"无法读取事件日志: {exc}") from exc
    events: list[dict] = []
    for line in raw_lines:
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        if event_type is not None and event.get("type") != event_type:
            continue
        events.append(event)
    if limit is not None:
        events = events[-limit:] if limit > 0 else []
    return events
