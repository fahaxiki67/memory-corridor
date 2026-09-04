"""Claude Code 原生 Hook 适配层。

与 Codex 适配层共用同一事件处理器（``handle_hook_event``）：两个平台的
hook 输入 payload 与输出 JSON 高度同源（``hook_event_name``/``cwd``/
``stop_hook_active`` 进，``decision``/``reason``/``systemMessage``/
``hookSpecificOutput.additionalContext`` 出）。平台差异只在配置文件：

- Claude Code 的配置在 ``<project>/.claude/settings.json``（其中通常还包含
  用户自己的 permissions 等内容，本模块只增删自己的 hook，其余原样保留）；
- Claude Code 没有 per-hook trust hash，而是工作区信任对话框：信任该目录前
  项目 hook 不运行；``/hooks`` 菜单可只读查看；``-p`` 非交互模式视为已信任；
- PreCompact 的 ``systemMessage`` 会被 Claude Code 丢弃（落盘副作用仍有效）。

本模块不实现 requirement / evidence / gate 业务规则，不解析 transcript。
"""

from __future__ import annotations

from pathlib import Path

from .codex import (
    HOOK_TIMEOUT_SECONDS,
    PRE_COMPACT_MATCHER,
    SESSION_START_MATCHER,
    handle_hook_event,
    run_hook_command,
)
from .hook_config import HookPlatform, install_platform_hooks, platform_hook_status, uninstall_platform_hooks

CLAUDE_DIR = ".claude"
SETTINGS_FILE_NAME = "settings.json"
CLAUDE_HOOK_COMMAND = "memory-corridor claude hook"

PLATFORM = HookPlatform(
    name="claude",
    config_path_name=Path(CLAUDE_DIR) / SETTINGS_FILE_NAME,
    command=CLAUDE_HOOK_COMMAND,
    timeout_seconds=HOOK_TIMEOUT_SECONDS,
    # Claude Code 不支持 statusMessage 字段：handler 保持 type/command/timeout 最小集合。
    status_messages={},
    matchers={"PreCompact": PRE_COMPACT_MATCHER, "SessionStart": SESSION_START_MATCHER, "Stop": None},
    extra_handler_fields={},
    fresh_top_level={"hooks": {}},
)

__all__ = [
    "CLAUDE_HOOK_COMMAND",
    "PLATFORM",
    "handle_claude_hook_event",
    "hooks_settings_path",
    "install_claude_hooks",
    "run_claude_hook_command",
    "uninstall_claude_hooks",
    "claude_hook_status",
]


def hooks_settings_path(root: Path) -> Path:
    return Path(root) / CLAUDE_DIR / SETTINGS_FILE_NAME


def install_claude_hooks(root: Path) -> dict:
    """把三个 Memory Corridor Hook 合并进 <root>/.claude/settings.json（幂等）。"""
    return install_platform_hooks(PLATFORM, root)


def uninstall_claude_hooks(root: Path) -> dict:
    """只移除 Memory Corridor 自己的 Hook，第三方 Hook 与用户配置原样保留（幂等）。"""
    return uninstall_platform_hooks(PLATFORM, root)


def claude_hook_status(root: Path) -> dict:
    """只读检查安装状态；Claude Code 的工作区信任状态没有公开接口，不猜测。"""
    return platform_hook_status(PLATFORM, root)


def handle_claude_hook_event(payload: object) -> object:
    """事件处理与 Codex 完全同源；保留独立入口以便未来平台差异分叉。"""
    return handle_hook_event(payload)


def run_claude_hook_command(stdin: object | None = None, stdout: object | None = None, stderr: object | None = None) -> int:
    """``memory-corridor claude hook`` 的入口（stdin JSON → stdout JSON）。"""
    return run_hook_command(stdin=stdin, stdout=stdout, stderr=stderr)
