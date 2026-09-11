"""ZCode 原生 Hook 适配层（Plugin 分发）。

只负责 ZCode Hook 协议（stdin JSON 进、stdout JSON 出）与现有
recovery / gate / contract API 之间的翻译。协议事实于 2026-09-11 以
ZCode 官方配置文档（zcode-configuration-guide / diagnosing-hooks /
diagnosing-plugins skills）和本机客户端实现（hook 运行器）核对：

1. ZCode 仅支持 7 种 hook 事件，**没有 PreCompact**；本层只接
   ``SessionStart`` 与 ``Stop``，恢复包始终由最新 state 现场重建，
   不存在"compact 前刷新"的需求（也从不读旧 recovery.md）。
2. SessionStart 的 match 值（输入字段 ``source``）为
   ``startup|resume|clear|compact``；matcher 是对其的区分大小写正则。
3. Stop 的输入含 ``stop_hook_active``；平台对 Stop 连续续命有硬上限
   （3 次）。本层只在 ``stop_hook_active`` 为假时阻塞一次，因此每回合
   至多请求 1 次续命，远低于平台上限；不依赖 Stop hook 实现无限运行。
4. stdout 输出必须是严格 schema 的 JSON（任何多余键都会让整份输出被
   丢弃）：continue / decision("approve"|"block") / reason / systemMessage /
   additionalContext(或 additional_context) / hookSpecificOutput /
   stopReason / suppressOutput。
5. **ZCode Stop 特例**：``continue: true`` 在 Stop 上会请求续命（与
   Codex/Claude 语义相反）。因此本层在放行场景一律不输出任何内容
   （exit 0 + 空 stdout），绝不写 ``{"continue": true}``。
6. 阻塞使用 ``{"decision": "block", "reason": ...}``：客户端会把它转为
   续命并把 reason 注入上下文；SessionStart 注入使用
   ``hookSpecificOutput: {hookEventName: "SessionStart", additionalContext}``。

与 Codex/Claude 适配的关键差异：放行输出为空而非 ``continue: true``；
不支持 PreCompact；安装不走项目配置文件，而是 ZCode Plugin
（``.zcode-plugin/plugin.json`` + ``hooks/hooks.json``，由 ZCode 官方
Plugin/Mechanism 分发，项目级配置 hooks 默认不启用）。

本模块不实现 requirement / evidence / gate 业务规则，不解析 transcript，
不自动产生 evidence，不自动修改 requirement。``.context-guard/state.json``
仍是唯一业务状态来源。
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

from ..contract import GuardError, ProjectPaths, project_paths
from ..gate import check_gate
from ..recovery import build_packet
from .codex import (
    HookOutcome,
    HookProtocolError,
    _record_hook_event,
    _render_blockers,
)
from .hook_config import read_hooks_config

HOOK_COMMAND = "memory-corridor zcode hook"
SUPPORTED_EVENTS = ("SessionStart", "Stop")
SESSION_START_MATCHER = "startup|resume|compact"
# startup/resume/compact 注入；clear 视为用户主动清空上下文，不注入旧工作状态。
INJECT_SOURCES = {"startup", "resume", "compact"}
STOP_CONTINUATION_LIMIT = 3  # ZCode 客户端硬限制；本层每回合最多只用 1 次
WRAPPER_RELATIVE_PATH = Path("hooks") / "zcode_hook.py"
HOOKS_RELATIVE_PATH = Path("hooks") / "hooks.json"
MANIFEST_RELATIVE_PATH = Path(".zcode-plugin") / "plugin.json"
HOOK_COMMAND_BASENAME = "python3"


# ---------------------------------------------------------------------------
# Hook 事件处理
# ---------------------------------------------------------------------------


def _payload_value(payload: dict, *keys: str) -> object:
    """ZCode 输入同时携带内部 camelCase 字段与 snake_case 兼容字段。"""
    for key in keys:
        if key in payload:
            return payload[key]
    return None


def handle_zcode_hook_event(payload: object) -> HookOutcome:
    if not isinstance(payload, dict):
        raise HookProtocolError("stdin 顶层必须是 JSON 对象")
    event = _payload_value(payload, "hook_event_name", "hookEventName")
    if not isinstance(event, str) or not event.strip():
        raise HookProtocolError("payload 缺少非空的 hook_event_name")
    cwd_value = payload.get("cwd")
    cwd = cwd_value if isinstance(cwd_value, str) and cwd_value.strip() else None
    paths = project_paths(cwd)
    if event == "SessionStart":
        return _handle_session_start(paths, payload)
    if event == "Stop":
        return _handle_stop(paths, payload)
    raise HookProtocolError(
        f"不支持的 hook_event_name: {event}；ZCode 仅支持 {', '.join(SUPPORTED_EVENTS)}"
        "（ZCode 没有 PreCompact 事件）"
    )


def run_zcode_hook_command(
    stdin: object | None = None,
    stdout: object | None = None,
    stderr: object | None = None,
) -> int:
    """``memory-corridor zcode hook`` 的入口：stdin JSON → stdout JSON。

    stdout 只允许出现符合 ZCode 严格 schema 的 Hook 输出（或什么都不写）；
    所有诊断走 stderr。项目根取自 payload 的 ``cwd`` 字段，与 hook 进程
    自身的工作目录无关。
    """
    stdin = sys.stdin if stdin is None else stdin
    stdout = sys.stdout if stdout is None else stdout
    stderr = sys.stderr if stderr is None else stderr
    if hasattr(stdin, "isatty") and stdin.isatty():
        print(
            "memory-corridor zcode hook：本命令由 ZCode Hook 调用，事件 JSON 应从 stdin 管道传入；"
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
        print(f"memory-corridor zcode hook：stdin 不是合法 JSON：{exc}", file=stderr)
        return 1
    try:
        outcome = handle_zcode_hook_event(payload)
    except HookProtocolError as exc:
        print(f"memory-corridor zcode hook：{exc}", file=stderr)
        return 1
    for line in outcome.diagnostics:
        print(line, file=stderr)
    if outcome.output is not None:
        stdout.write(json.dumps(outcome.output, ensure_ascii=False) + "\n")
    return outcome.exit_code


def _handle_session_start(paths: ProjectPaths, payload: dict) -> HookOutcome:
    source = payload.get("source")
    if not paths.state.exists():
        return HookOutcome(
            output=None,
            diagnostics=(f"Memory Corridor: {paths.root} 未初始化，SessionStart 按约定 no-op。",),
        )
    # clear 视为用户主动清空上下文，不注入旧工作状态（与 Codex/Claude 适配同一取舍）。
    if source not in INJECT_SOURCES:
        _record_hook_event(
            paths, "hook.session_start", {"platform": "zcode", "source": source, "result": "skipped"}
        )
        return HookOutcome(
            output=None,
            diagnostics=(f"Memory Corridor: SessionStart source={source!r}，不注入恢复包。",),
        )
    try:
        packet = build_packet(paths)
    except (GuardError, OSError) as exc:
        # 恢复失败时不得伪造恢复包；输出保持为空，诊断走 stderr 与事件日志。
        _record_hook_event(
            paths, "hook.session_start", {"platform": "zcode", "source": source, "result": "failed"}
        )
        return HookOutcome(
            output=None,
            diagnostics=(f"Memory Corridor: 恢复包构建失败：{exc}",),
        )
    _record_hook_event(
        paths, "hook.session_start", {"platform": "zcode", "source": source, "result": "injected"}
    )
    return HookOutcome(
        output={
            "hookSpecificOutput": {
                "hookEventName": "SessionStart",
                "additionalContext": packet,
            },
        }
    )


def _stop_outcome_for_gate(gate: dict, stop_hook_active: bool) -> HookOutcome:
    """按门禁结果决定 Stop 输出（ZCode 版）。

    放行场景输出为空（``output=None``）：ZCode 在 Stop 上把
    ``continue: true`` 解释为"请求续命"，因此绝不能照搬 Codex/Claude 的
    放行输出。阻塞只在 ``stop_hook_active`` 为假时发生一次。
    """
    if gate["status"] in ("disabled", "idle") or gate["ok"]:
        return HookOutcome(output=None)
    if stop_hook_active:
        # 平台最多允许 3 次续命；本适配只阻塞一次，其余交回给模型/用户，
        # 避免无限 continuation loop。
        ids = ", ".join(item["requirement_id"] for item in gate["blocking"])
        return HookOutcome(
            output=None,
            diagnostics=(
                "Memory Corridor: 完成门禁仍未通过（"
                f"{ids}）；本轮已续命过一次，不再阻塞，请取得真实验证证据后结束。",
            ),
        )
    reason = (
        "不能结束。Memory Corridor 完成门禁未通过：\n\n"
        f"{_render_blockers(gate)}\n\n"
        "请继续处理这些 requirement，取得真实验证证据后再尝试结束。"
    )
    return HookOutcome(output={"decision": "block", "reason": reason})


def _handle_stop(paths: ProjectPaths, payload: dict) -> HookOutcome:
    stop_hook_active = bool(_payload_value(payload, "stop_hook_active", "stopHookActive"))
    if not paths.state.exists():
        return HookOutcome(
            output=None,
            diagnostics=(f"Memory Corridor: {paths.root} 未初始化，Stop 门禁不生效。",),
        )
    try:
        gate = check_gate(paths)
    except (GuardError, OSError) as exc:
        # 无法判断不得伪装为业务 PASS；首次阻塞一次并给出可执行指引，
        # stop_hook_active 为真时不再续命。
        _record_hook_event(
            paths,
            "hook.stop",
            {
                "platform": "zcode",
                "decision": "block" if not stop_hook_active else "allow",
                "gate_status": "unreadable",
                "blocking_count": 0,
                "stop_hook_active": stop_hook_active,
            },
        )
        if stop_hook_active:
            return HookOutcome(
                output=None,
                diagnostics=(f"Memory Corridor: 状态无法读取：{exc}",),
            )
        return HookOutcome(
            output={
                "decision": "block",
                "reason": (
                    "不能结束。Memory Corridor 的状态无法读取，"
                    "「读不了」不会被当成通过。\n\n"
                    f"{exc}\n\n"
                    "请检查 .context-guard/state.json（可运行 memory-corridor status），"
                    "修复或用 memory-corridor off 关闭保护后再结束。"
                ),
            },
            diagnostics=(f"Memory Corridor: 状态无法读取：{exc}",),
        )
    outcome = _stop_outcome_for_gate(gate, stop_hook_active)
    _record_hook_event(
        paths,
        "hook.stop",
        {
            "platform": "zcode",
            "decision": "block" if outcome.output is not None else "allow",
            "gate_status": gate["status"],
            "blocking_count": len(gate["blocking"]),
            "stop_hook_active": stop_hook_active,
        },
    )
    return outcome


# ---------------------------------------------------------------------------
# 状态检查（只读；ZCode 没有 install/uninstall 配置命令——hook 由 Plugin 分发）
# ---------------------------------------------------------------------------


def plugin_files_status(root: Path) -> dict:
    """检查仓库内 Plugin 分发文件（plugin.json / hooks.json / wrapper）。"""
    root = Path(root)
    result: dict = {
        "manifest_path": str(root / MANIFEST_RELATIVE_PATH),
        "manifest_exists": False,
        "manifest_valid": None,
        "manifest_error": None,
        "manifest_name": None,
        "hooks_file_path": str(root / HOOKS_RELATIVE_PATH),
        "hooks_file_exists": False,
        "hooks_file_valid": None,
        "hooks_file_error": None,
        "events": {},
        "wrapper_path": str(root / WRAPPER_RELATIVE_PATH),
        "wrapper_exists": False,
    }
    manifest_path = root / MANIFEST_RELATIVE_PATH
    if manifest_path.exists():
        result["manifest_exists"] = True
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
            if not isinstance(manifest, dict) or not isinstance(manifest.get("name"), str):
                raise ValueError("manifest 顶层必须是含 name 字符串字段的对象")
            result["manifest_valid"] = True
            result["manifest_name"] = manifest["name"]
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            result["manifest_valid"] = False
            result["manifest_error"] = str(exc)
    hooks_path = root / HOOKS_RELATIVE_PATH
    if hooks_path.exists():
        result["hooks_file_exists"] = True
        try:
            config = read_hooks_config(hooks_path)
            result["hooks_file_valid"] = True
        except GuardError as exc:
            result["hooks_file_error"] = str(exc)
            return result
        hooks = config.get("hooks", {})
        for event in SUPPORTED_EVENTS:
            groups = hooks.get(event, [])
            configured = False
            matcher = None
            for group in groups:
                for handler in group.get("hooks", []):
                    if isinstance(handler, dict) and HOOK_COMMAND_BASENAME == handler.get("command"):
                        configured = True
                        matcher = group.get("matcher")
            result["events"][event] = {
                "configured": configured,
                "matcher": matcher,
                "matcher_expected": SESSION_START_MATCHER if event == "SessionStart" else None,
            }
    wrapper_path = root / WRAPPER_RELATIVE_PATH
    result["wrapper_exists"] = wrapper_path.exists()
    return result


def installed_plugin_status() -> dict:
    """只读扫描 ZCode 插件缓存，查找已安装的 memory-corridor 插件。

    缓存布局为 ``<home>/.zcode/cli/plugins/cache/<marketplace>/<plugin>/<version>/``；
    找到 ``.zcode-plugin/plugin.json`` 且 name 匹配即认为已安装。找不到不代表
    未安装（布局变化时如实返回 not_found），不猜测启用状态。
    """
    cache_root = Path.home() / ".zcode" / "cli" / "plugins" / "cache"
    found: list[dict] = []
    if cache_root.is_dir():
        for manifest_path in sorted(cache_root.glob("*/*/*/.zcode-plugin/plugin.json")):
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
            except (OSError, json.JSONDecodeError):
                continue
            if isinstance(manifest, dict) and manifest.get("name") == "memory-corridor":
                plugin_root = manifest_path.parent.parent
                found.append(
                    {
                        "path": str(plugin_root),
                        "version": manifest.get("version"),
                        "hooks_file_exists": (plugin_root / HOOKS_RELATIVE_PATH).exists(),
                    }
                )
    return {
        "cache_root": str(cache_root),
        "installed": bool(found),
        "copies": found,
    }


def zcode_hook_status(root: Path) -> dict:
    """``memory-corridor zcode status`` 的只读检查结果。"""
    paths = project_paths(root)
    protection_enabled: bool | None = None
    if paths.state.exists():
        try:
            protection_enabled = _read_enabled(paths)
        except (GuardError, OSError):
            protection_enabled = None
    return {
        "platform": "zcode",
        "plugin_files": plugin_files_status(paths.root),
        "installed": installed_plugin_status(),
        "python3_on_path": shutil.which(HOOK_COMMAND_BASENAME) is not None,
        "memory_corridor_on_path": shutil.which("memory-corridor") is not None,
        "project_initialized": paths.state.exists(),
        "protection_enabled": protection_enabled,
        "protocol": {
            "supported_events": list(SUPPORTED_EVENTS),
            "pre_compact_supported": False,
            "session_start_matcher": SESSION_START_MATCHER,
            "inject_sources": sorted(INJECT_SOURCES),
            "stop_continuation_limit": STOP_CONTINUATION_LIMIT,
            "pass_output": "empty（ZCode Stop 上 continue:true 会触发续命，放行必须输出空）",
            "session_start_e2e_verified": True,
            "session_start_e2e_note": "2026-09-11 在真实 ZCode CLI 0.16.5（headless --prompt）验证注入链路",
            "stop_e2e_verified": True,
            "stop_e2e_note": "2026-09-12 在真实 ZCode 桌面客户端验证：block（stop_hook_active=false）→续命→allow（防循环）→PASS allow 全链路，见 events.jsonl",
        },
    }


def _read_enabled(paths: ProjectPaths) -> bool:
    from ..contract import load_state

    return bool(load_state(paths)["contract"].get("enabled", False))


__all__ = [
    "HOOK_COMMAND",
    "SUPPORTED_EVENTS",
    "STOP_CONTINUATION_LIMIT",
    "handle_zcode_hook_event",
    "installed_plugin_status",
    "plugin_files_status",
    "run_zcode_hook_command",
    "zcode_hook_status",
]
