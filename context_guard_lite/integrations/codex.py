"""Codex 原生 Hook 适配层。

只负责 Codex Hook 协议（stdin JSON 进、stdout JSON 出）与现有
recovery / gate / contract API 之间的翻译：

1. 从 stdin 读取 Codex Hook JSON；
2. 校验最低必要字段；
3. 按 ``hook_event_name`` dispatch；
4. 调用现有 recovery / gate API；
5. 输出符合 Codex Hook schema 的 JSON（stdout），诊断信息走 stderr。

本模块不实现 requirement / evidence / gate 业务规则，不解析 transcript，
不自动产生 evidence，不自动修改 requirement。
"""

from __future__ import annotations

import json
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

from ..contract import GuardError, ProjectPaths, atomic_write, project_paths
from ..gate import check_gate
from ..recovery import build_packet, write_packet


HOOK_COMMAND = "memory-corridor codex hook"
HOOK_TYPE = "command"
HOOK_TIMEOUT_SECONDS = 30
SUPPORTED_EVENTS = ("PreCompact", "SessionStart", "Stop")
SESSION_START_MATCHER = "resume|compact"
PRE_COMPACT_MATCHER = "manual|auto"
RESUME_SOURCES = {"resume", "compact"}
ADDITIONAL_CONTEXT_LIMIT = 4000
MAX_BLOCKERS_IN_REASON = 10
BACKUP_SUFFIX = ".bak"
CODEX_DIR = ".codex"
HOOKS_FILE_NAME = "hooks.json"
MANAGED_DESCRIPTION = (
    "Managed by Memory Corridor (memory-corridor codex install). "
    "Third-party hooks are preserved; see https://github.com/fahaxiki67/memory-corridor"
)
STATUS_MESSAGES = {
    "PreCompact": "Refreshing Memory Corridor recovery packet",
    "SessionStart": "Restoring Memory Corridor working state",
    "Stop": "Checking Memory Corridor completion gate",
}


class HookProtocolError(ValueError):
    """stdin payload 不符合 Codex Hook 协议的最低要求。"""


@dataclass(frozen=True)
class HookOutcome:
    """单个 Hook 事件的处理结果。output 为 None 表示 stdout 不写任何内容。"""

    output: dict | None
    exit_code: int = 0
    diagnostics: tuple[str, ...] = ()


def hooks_config_path(root: Path) -> Path:
    return root / CODEX_DIR / HOOKS_FILE_NAME


# ---------------------------------------------------------------------------
# Hook 事件处理
# ---------------------------------------------------------------------------


def handle_hook_event(payload: object) -> HookOutcome:
    if not isinstance(payload, dict):
        raise HookProtocolError("stdin 顶层必须是 JSON 对象")
    event = payload.get("hook_event_name")
    if not isinstance(event, str) or not event.strip():
        raise HookProtocolError("payload 缺少非空的 hook_event_name")
    cwd_value = payload.get("cwd")
    cwd = cwd_value if isinstance(cwd_value, str) and cwd_value.strip() else None
    paths = project_paths(cwd)
    if event == "PreCompact":
        return _handle_pre_compact(paths)
    if event == "SessionStart":
        return _handle_session_start(paths, payload)
    if event == "Stop":
        return _handle_stop(paths, payload)
    raise HookProtocolError(
        f"不支持的 hook_event_name: {event}；支持：{', '.join(SUPPORTED_EVENTS)}"
    )


def run_hook_command(stdin: object | None = None, stdout: object | None = None, stderr: object | None = None) -> int:
    """``memory-corridor codex hook`` 的入口：stdin JSON → stdout JSON。

    stdout 只允许出现合法 Hook 输出；所有诊断走 stderr。
    """
    stdin = sys.stdin if stdin is None else stdin
    stdout = sys.stdout if stdout is None else stdout
    stderr = sys.stderr if stderr is None else stderr
    if hasattr(stdin, "isatty") and stdin.isatty():
        print(
            "memory-corridor codex hook：本命令由 Codex Hook 调用，事件 JSON 应从 stdin 管道传入；"
            "请不要在交互终端直接运行。",
            file=stderr,
        )
        return 1
    raw = getattr(stdin, "buffer", stdin).read()
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", errors="replace")
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        print(f"memory-corridor codex hook：stdin 不是合法 JSON：{exc}", file=stderr)
        return 1
    try:
        outcome = handle_hook_event(payload)
    except HookProtocolError as exc:
        print(f"memory-corridor codex hook：{exc}", file=stderr)
        return 1
    for line in outcome.diagnostics:
        print(line, file=stderr)
    if outcome.output is not None:
        stdout.write(json.dumps(outcome.output, ensure_ascii=False) + "\n")
    return outcome.exit_code


def _handle_pre_compact(paths: ProjectPaths) -> HookOutcome:
    if not paths.state.exists():
        return HookOutcome(
            output={"continue": True},
            diagnostics=(f"Memory Corridor: {paths.root} 未初始化，PreCompact 按约定 no-op。",),
        )
    try:
        packet = build_packet(paths)
        target = write_packet(paths, content=packet)
    except (GuardError, OSError) as exc:
        return HookOutcome(
            output={
                "continue": True,
                "systemMessage": f"Memory Corridor: could not refresh recovery packet: {exc}",
            },
            diagnostics=(f"Memory Corridor: 恢复包刷新失败：{exc}",),
        )
    return HookOutcome(
        output={
            "continue": True,
            "systemMessage": f"Memory Corridor: recovery packet refreshed ({target}).",
        }
    )


def _handle_session_start(paths: ProjectPaths, payload: dict) -> HookOutcome:
    source = payload.get("source")
    if not paths.state.exists():
        return HookOutcome(
            output={"continue": True},
            diagnostics=(f"Memory Corridor: {paths.root} 未初始化，SessionStart 按约定 no-op。",),
        )
    # startup/clear 用户可能就是想清空上下文，不注入旧工作状态。
    if source not in RESUME_SOURCES:
        return HookOutcome(
            output={"continue": True},
            diagnostics=(f"Memory Corridor: SessionStart source={source!r}，不注入恢复包。",),
        )
    try:
        packet = build_packet(paths)
    except (GuardError, OSError) as exc:
        # 恢复失败时不得伪造恢复包。
        return HookOutcome(
            output={
                "continue": True,
                "systemMessage": f"Memory Corridor: could not build recovery packet: {exc}",
            },
            diagnostics=(f"Memory Corridor: 恢复包构建失败：{exc}",),
        )
    return HookOutcome(
        output={
            "continue": True,
            "hookSpecificOutput": {
                "hookEventName": "SessionStart",
                "additionalContext": packet,
            },
        }
    )


def _handle_stop(paths: ProjectPaths, payload: dict) -> HookOutcome:
    stop_hook_active = bool(payload.get("stop_hook_active", False))
    if not paths.state.exists():
        return HookOutcome(
            output={"continue": True},
            diagnostics=(f"Memory Corridor: {paths.root} 未初始化，Stop 门禁不生效。",),
        )
    try:
        gate = check_gate(paths)
    except (GuardError, OSError) as exc:
        # 无法判断不得伪装为业务 PASS；首次遇到时阻塞一次并给出可执行指引，
        # stop_hook_active 为真时不再续命，避免无限 continuation loop。
        if stop_hook_active:
            return HookOutcome(
                output={
                    "continue": True,
                    "systemMessage": f"Memory Corridor: gate state unreadable, gate not enforced ({exc}).",
                },
                diagnostics=(f"Memory Corridor: 状态无法读取：{exc}",),
            )
        return HookOutcome(
            output={
                "decision": "block",
                "reason": (
                    "Memory Corridor completion gate could not be read; "
                    "an unreadable state is not treated as a pass.\n\n"
                    f"{exc}\n\n"
                    "Inspect .context-guard/state.json, fix or set the guard off, "
                    "then run memory-corridor status."
                ),
            },
            diagnostics=(f"Memory Corridor: 状态无法读取：{exc}",),
        )
    if gate["status"] == "disabled":
        return HookOutcome(
            output={"continue": True},
            diagnostics=("Memory Corridor: 保护已关闭，完成门禁不生效。",),
        )
    if gate["ok"]:
        return HookOutcome(output={"continue": True})
    blockers = _render_blockers(gate)
    if stop_hook_active:
        ids = ", ".join(item["requirement_id"] for item in gate["blocking"])
        return HookOutcome(
            output={
                "continue": True,
                "systemMessage": (
                    f"Memory Corridor: completion gate still blocked ({ids}). "
                    "Not continuing again to avoid a loop; resolve blockers and record valid evidence."
                ),
            }
        )
    return HookOutcome(
        output={
            "decision": "block",
            "reason": (
                "Memory Corridor completion gate is blocked.\n\n"
                f"{blockers}\n\n"
                "Resolve these blockers, record valid evidence, then run the completion gate again."
            ),
        }
    )


def _render_blockers(gate: dict) -> str:
    lines: list[str] = []
    blocking = gate["blocking"]
    for item in blocking[:MAX_BLOCKERS_IN_REASON]:
        if "requirement_id" in item:
            lines.append(f"{item['requirement_id']} (v{item.get('revision', 1)}):")
            lines.extend(f"- {reason}" for reason in item["reasons"])
        else:
            # 例如 no_requirements：没有 active requirement 时不能宣称完成。
            lines.append(f"- {item.get('reason')}")
    if len(blocking) > MAX_BLOCKERS_IN_REASON:
        lines.append(
            f"… and {len(blocking) - MAX_BLOCKERS_IN_REASON} more blocked requirements not listed; "
            "run `memory-corridor gate check` for the full list."
        )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# hooks.json 配置管理（仅 project scope：<project>/.codex/hooks.json）
# ---------------------------------------------------------------------------


def _handler_for(event: str) -> dict:
    handler: dict = {
        "type": HOOK_TYPE,
        "command": HOOK_COMMAND,
        "timeout": HOOK_TIMEOUT_SECONDS,
        "statusMessage": STATUS_MESSAGES[event],
    }
    if event == "SessionStart":
        handler["additionalContextLimit"] = ADDITIONAL_CONTEXT_LIMIT
    return handler


def _group_for(event: str) -> dict:
    group: dict = {"hooks": [_handler_for(event)]}
    if event == "SessionStart":
        group["matcher"] = SESSION_START_MATCHER
    elif event == "PreCompact":
        group["matcher"] = PRE_COMPACT_MATCHER
    return group


def _is_memory_corridor_handler(handler: object) -> bool:
    return isinstance(handler, dict) and handler.get("type") == HOOK_TYPE and handler.get("command") == HOOK_COMMAND


def _contains_memory_corridor(groups: list) -> bool:
    for group in groups:
        if not isinstance(group, dict):
            continue
        for handler in group.get("hooks", []):
            if _is_memory_corridor_handler(handler):
                return True
    return False


def _validate_config(config: object, path: Path) -> None:
    if not isinstance(config, dict):
        raise GuardError(f"{path} 顶层必须是 JSON 对象")
    hooks = config.get("hooks", {})
    if not isinstance(hooks, dict):
        raise GuardError(f'{path} 的 "hooks" 必须是 JSON 对象')
    for event, groups in hooks.items():
        if not isinstance(groups, list):
            raise GuardError(f"{path} 事件 {event} 必须是列表")
        for index, group in enumerate(groups):
            if not isinstance(group, dict):
                raise GuardError(f"{path} 事件 {event} 第 {index} 组必须是对象")
            entries = group.get("hooks", [])
            if not isinstance(entries, list):
                raise GuardError(f'{path} 事件 {event} 第 {index} 组的 "hooks" 必须是列表')


def _read_hooks_config(path: Path) -> dict:
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise GuardError(f"无法读取 {path}：{exc}") from exc
    try:
        config = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise GuardError(f"{path} 不是合法 JSON，已停止操作且不修改原文件：{exc}") from exc
    _validate_config(config, path)
    return config


def _write_backup(path: Path) -> Path:
    # 单份滚动备份：始终覆盖同名 .bak，不无限生成。
    backup = path.with_name(path.name + BACKUP_SUFFIX)
    atomic_write(backup, path.read_text(encoding="utf-8"))
    return backup


def _write_hooks_config(path: Path, config: dict) -> None:
    atomic_write(path, json.dumps(config, ensure_ascii=False, indent=2) + "\n")


def install_hooks(root: Path) -> dict:
    """把三个 Memory Corridor Hook 合并进 <root>/.codex/hooks.json（幂等）。"""
    path = hooks_config_path(root)
    if path.exists():
        config = _read_hooks_config(path)
        existed = True
    else:
        config = {"description": MANAGED_DESCRIPTION, "hooks": {}}
        existed = False
    hooks = config.setdefault("hooks", {})
    added: list[str] = []
    already: list[str] = []
    for event in SUPPORTED_EVENTS:
        groups = hooks.setdefault(event, [])
        if _contains_memory_corridor(groups):
            already.append(event)
            continue
        groups.append(_group_for(event))
        added.append(event)
    if not added:
        return {"added": [], "already": already, "path": str(path), "written": False, "backup": None}
    backup = _write_backup(path) if existed else None
    _write_hooks_config(path, config)
    return {
        "added": added,
        "already": already,
        "path": str(path),
        "written": True,
        "backup": str(backup) if backup else None,
    }


def uninstall_hooks(root: Path) -> dict:
    """只移除 Memory Corridor 自己的 Hook，第三方 Hook 原样保留（幂等）。"""
    path = hooks_config_path(root)
    if not path.exists():
        return {"removed": [], "path": str(path), "written": False, "backup": None, "absent": True}
    config = _read_hooks_config(path)
    hooks = config.get("hooks", {})
    removed: set[str] = set()
    for event, groups in list(hooks.items()):
        kept_groups = []
        for group in groups:
            entries = group.get("hooks", [])
            kept = [handler for handler in entries if not _is_memory_corridor_handler(handler)]
            if len(kept) != len(entries):
                removed.add(event)
            if kept:
                group["hooks"] = kept
                kept_groups.append(group)
        if kept_groups:
            hooks[event] = kept_groups
        else:
            del hooks[event]
    if not removed:
        return {"removed": [], "path": str(path), "written": False, "backup": None}
    backup = _write_backup(path)
    _write_hooks_config(path, config)
    return {
        "removed": sorted(removed),
        "path": str(path),
        "written": True,
        "backup": str(backup),
    }


EXPECTED_MATCHERS = {
    "PreCompact": PRE_COMPACT_MATCHER,
    "SessionStart": SESSION_START_MATCHER,
}


def hook_status(root: Path) -> dict:
    """只读检查安装状态。Codex 内部 trust 状态没有公开接口，不猜测。"""
    paths = project_paths(root)
    path = hooks_config_path(paths.root)
    result: dict = {
        "hooks_file": str(path),
        "hooks_file_exists": path.exists(),
        "hooks_file_valid": None,
        "hooks_file_error": None,
        "events": {},
        "project_initialized": paths.state.exists(),
        "command_on_path": shutil.which("memory-corridor") is not None,
        "trust": "unable to determine automatically",
    }
    if not path.exists():
        return result
    try:
        config = _read_hooks_config(path)
    except GuardError as exc:
        result["hooks_file_valid"] = False
        result["hooks_file_error"] = str(exc)
        return result
    result["hooks_file_valid"] = True
    hooks = config.get("hooks", {})
    for event in SUPPORTED_EVENTS:
        expected_matcher = EXPECTED_MATCHERS.get(event)
        entry = {
            "configured": False,
            "matcher": None,
            "matcher_expected": expected_matcher,
            "matcher_drifted": False,
            "third_party_present": False,
        }
        for group in hooks.get(event, []):
            handlers = group.get("hooks", [])
            if any(_is_memory_corridor_handler(handler) for handler in handlers):
                entry["configured"] = True
                entry["matcher"] = group.get("matcher")
            if any(not _is_memory_corridor_handler(handler) for handler in handlers):
                entry["third_party_present"] = True
        if not entry["configured"] and hooks.get(event):
            entry["third_party_present"] = True
        if entry["configured"] and expected_matcher is not None:
            # matcher 被手动改动会导致 hook 触发条件悄然变化，status 需明示。
            entry["matcher_drifted"] = entry["matcher"] != expected_matcher
        result["events"][event] = entry
    return result
