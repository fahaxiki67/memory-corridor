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

import contextlib
import json
import sys
from dataclasses import dataclass
from pathlib import Path

from ..contract import GuardError, ProjectPaths, append_event, project_paths
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


# ---------------------------------------------------------------------------
# Hook 事件处理
# ---------------------------------------------------------------------------


def handle_hook_event(payload: object, platform: str = "codex") -> HookOutcome:
    if not isinstance(payload, dict):
        raise HookProtocolError("stdin 顶层必须是 JSON 对象")
    event = payload.get("hook_event_name")
    if not isinstance(event, str) or not event.strip():
        raise HookProtocolError("payload 缺少非空的 hook_event_name")
    cwd_value = payload.get("cwd")
    cwd = cwd_value if isinstance(cwd_value, str) and cwd_value.strip() else None
    paths = project_paths(cwd)
    if event == "PreCompact":
        outcome = _handle_pre_compact(paths, platform)
    elif event == "SessionStart":
        outcome = _handle_session_start(paths, payload, platform)
    elif event == "Stop":
        outcome = _handle_stop(paths, payload, platform)
    else:
        raise HookProtocolError(
            f"不支持的 hook_event_name: {event}；支持：{', '.join(SUPPORTED_EVENTS)}"
        )
    return outcome


def run_hook_command(
    stdin: object | None = None,
    stdout: object | None = None,
    stderr: object | None = None,
    platform: str = "codex",
) -> int:
    """``memory-corridor codex hook`` 的入口：stdin JSON → stdout JSON。

    stdout 只允许出现合法 Hook 输出；所有诊断走 stderr。
    platform 只用于事件审计（hook.* 事件里的 platform 字段），
    Claude 集成经 ``run_claude_hook_command`` 传入 "claude"。
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
        outcome = handle_hook_event(payload, platform=platform)
    except HookProtocolError as exc:
        print(f"memory-corridor codex hook：{exc}", file=stderr)
        return 1
    for line in outcome.diagnostics:
        print(line, file=stderr)
    if outcome.output is not None:
        stdout.write(json.dumps(outcome.output, ensure_ascii=False) + "\n")
    return outcome.exit_code


def _record_hook_event(paths: ProjectPaths, event_type: str, details: dict) -> None:
    """把 hook 触发记录进 events.jsonl，让门禁链路可观测、可验证。

    只在已初始化的项目记录（state.json 存在）；未初始化保持 no-op 承诺，
    不因 hook 触发而在无关目录落任何文件。审计写入是 best-effort：
    记事件失败绝不能反过来破坏门禁决策，故吞掉 OSError。
    """
    if not paths.state.exists():
        return
    with contextlib.suppress(OSError):
        append_event(paths, event_type, details)


def _handle_pre_compact(paths: ProjectPaths, platform: str = "codex") -> HookOutcome:
    if not paths.state.exists():
        return HookOutcome(
            output={"continue": True},
            diagnostics=(f"Memory Corridor: {paths.root} 未初始化，PreCompact 按约定 no-op。",),
        )
    try:
        packet = build_packet(paths)
        target = write_packet(paths, content=packet)
    except (GuardError, OSError) as exc:
        _record_hook_event(paths, "hook.pre_compact", {"platform": platform, "result": "failed"})
        return HookOutcome(
            output={
                "continue": True,
                "systemMessage": f"Memory Corridor: could not refresh recovery packet: {exc}",
            },
            diagnostics=(f"Memory Corridor: 恢复包刷新失败：{exc}",),
        )
    _record_hook_event(
        paths, "hook.pre_compact", {"platform": platform, "result": "refreshed"}
    )
    return HookOutcome(
        output={
            "continue": True,
            "systemMessage": f"Memory Corridor: recovery packet refreshed ({target}).",
        }
    )


def _handle_session_start(paths: ProjectPaths, payload: dict, platform: str = "codex") -> HookOutcome:
    source = payload.get("source")
    if not paths.state.exists():
        return HookOutcome(
            output={"continue": True},
            diagnostics=(f"Memory Corridor: {paths.root} 未初始化，SessionStart 按约定 no-op。",),
        )
    # startup/clear 用户可能就是想清空上下文，不注入旧工作状态。
    if source not in RESUME_SOURCES:
        _record_hook_event(
            paths, "hook.session_start", {"platform": platform, "source": source, "result": "skipped"}
        )
        return HookOutcome(
            output={"continue": True},
            diagnostics=(f"Memory Corridor: SessionStart source={source!r}，不注入恢复包。",),
        )
    try:
        packet = build_packet(paths)
    except (GuardError, OSError) as exc:
        # 恢复失败时不得伪造恢复包。
        _record_hook_event(
            paths, "hook.session_start", {"platform": platform, "source": source, "result": "failed"}
        )
        return HookOutcome(
            output={
                "continue": True,
                "systemMessage": f"Memory Corridor: could not build recovery packet: {exc}",
            },
            diagnostics=(f"Memory Corridor: 恢复包构建失败：{exc}",),
        )
    _record_hook_event(
        paths, "hook.session_start", {"platform": platform, "source": source, "result": "injected"}
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


def _stop_outcome_for_gate(gate: dict, stop_hook_active: bool) -> HookOutcome:
    """按门禁结果决定 Stop 输出；纯函数，事件记录由 _handle_stop 负责。"""
    if gate["status"] == "disabled":
        return HookOutcome(
            output={"continue": True},
            diagnostics=("Memory Corridor: 保护已关闭，完成门禁不生效。",),
        )
    if gate["status"] == "idle":
        # 空账本不再阻塞：放行并附引导，把最糟的第一印象变成 onboarding。
        return HookOutcome(
            output={
                "continue": True,
                "systemMessage": (
                    "Memory Corridor is installed but the ledger is empty. "
                    "Record what this task must achieve with "
                    '`memory-corridor requirements add "<requirement>"` '
                    "to enable the completion gate."
                ),
            }
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


def _handle_stop(paths: ProjectPaths, payload: dict, platform: str = "codex") -> HookOutcome:
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
        _record_hook_event(
            paths,
            "hook.stop",
            {
                "platform": platform,
                "decision": "block" if not stop_hook_active else "allow",
                "gate_status": "unreadable",
                "blocking_count": 0,
                "stop_hook_active": stop_hook_active,
            },
        )
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
    outcome = _stop_outcome_for_gate(gate, stop_hook_active)
    _record_hook_event(
        paths,
        "hook.stop",
        {
            "platform": platform,
            "decision": "block" if outcome.output.get("decision") == "block" else "allow",
            "gate_status": gate["status"],
            "blocking_count": len(gate["blocking"]),
            "stop_hook_active": stop_hook_active,
        },
    )
    return outcome


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
# 实现委托给通用引擎 hook_config，Claude Code 集成共用同一套逻辑。
# ---------------------------------------------------------------------------

from .hook_config import HookPlatform, install_platform_hooks, platform_hook_status, uninstall_platform_hooks  # noqa: E402

PLATFORM = HookPlatform(
    name="codex",
    config_path_name=Path(CODEX_DIR) / HOOKS_FILE_NAME,
    command=HOOK_COMMAND,
    timeout_seconds=HOOK_TIMEOUT_SECONDS,
    status_messages=STATUS_MESSAGES,
    matchers={"PreCompact": PRE_COMPACT_MATCHER, "SessionStart": SESSION_START_MATCHER, "Stop": None},
    extra_handler_fields={"SessionStart": {"additionalContextLimit": ADDITIONAL_CONTEXT_LIMIT}},
    fresh_top_level={"description": MANAGED_DESCRIPTION, "hooks": {}},
)

# 兼容旧引用：matcher 预期表（漂移检测的对外常量）。
EXPECTED_MATCHERS = {event: matcher for event, matcher in PLATFORM.matchers.items() if matcher is not None}


def hooks_config_path(root: Path) -> Path:
    return Path(root) / CODEX_DIR / HOOKS_FILE_NAME


def install_hooks(root: Path) -> dict:
    """把三个 Memory Corridor Hook 合并进 <root>/.codex/hooks.json（幂等）。"""
    return install_platform_hooks(PLATFORM, root)


def uninstall_hooks(root: Path) -> dict:
    """只移除 Memory Corridor 自己的 Hook，第三方 Hook 原样保留（幂等）。"""
    return uninstall_platform_hooks(PLATFORM, root)


def hook_status(root: Path) -> dict:
    """只读检查安装状态。Codex 内部 trust 状态没有公开接口，不猜测。"""
    return platform_hook_status(PLATFORM, root)
