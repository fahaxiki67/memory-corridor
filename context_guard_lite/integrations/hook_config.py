"""跨平台 Hook 配置管理引擎。

Codex（``.codex/hooks.json``）与 Claude Code（``.claude/settings.json``）的
hook 配置结构高度同源：事件名 -> matcher 分组列表 -> handler 列表。
本模块把合并/卸载/状态检查的通用逻辑参数化，两个平台各给一个
:class:`HookPlatform` 描述。

共同约束（对两个平台都成立）：
- 只写 project scope，绝不改用户全局配置；
- 第三方 hook 原样保留，只追加/移除自己的 entries；
- 幂等：重复 install 不重复添加；
- 原文件非法 JSON 时拒绝操作且不覆盖；
- 写入前生成单份滚动备份 ``<file>.bak``。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from ..contract import GuardError, atomic_write, project_paths

BACKUP_SUFFIX = ".bak"


@dataclass(frozen=True)
class HookPlatform:
    """一个 AI CLI 平台的 hook 配置描述。"""

    name: str
    config_path_name: Path  # 相对项目根的配置文件路径（如 .codex/hooks.json）
    command: str  # 本平台的 hook 命令（用于识别自己的 handler）
    timeout_seconds: int
    status_messages: dict[str, str]
    matchers: dict[str, str | None]  # event -> matcher（None=不写 matcher）
    extra_handler_fields: dict[str, dict] = field(default_factory=dict)
    # 新建配置文件时的顶层结构；已有文件则原样保留第三方内容。
    fresh_top_level: dict = field(default_factory=dict)
    managed_note: str = ""


def _is_managed_handler(handler: object, platform: HookPlatform) -> bool:
    return (
        isinstance(handler, dict)
        and handler.get("type") == "command"
        and handler.get("command") == platform.command
    )


def _handler_for(platform: HookPlatform, event: str) -> dict:
    handler: dict = {
        "type": "command",
        "command": platform.command,
        "timeout": platform.timeout_seconds,
    }
    for key, value in platform.extra_handler_fields.get(event, {}).items():
        handler[key] = value
    if platform.status_messages.get(event):
        handler["statusMessage"] = platform.status_messages[event]
    return handler


def _group_for(platform: HookPlatform, event: str) -> dict:
    group: dict = {"hooks": [_handler_for(platform, event)]}
    matcher = platform.matchers.get(event)
    if matcher:
        group["matcher"] = matcher
    return group


def _contains_managed(groups: list, platform: HookPlatform) -> bool:
    for group in groups:
        if not isinstance(group, dict):
            continue
        for handler in group.get("hooks", []):
            if _is_managed_handler(handler, platform):
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


def read_hooks_config(path: Path) -> dict:
    # utf-8-sig：容忍 Windows 记事本等编辑器留下的 UTF-8 BOM；写入时始终不带 BOM。
    try:
        raw = path.read_text(encoding="utf-8-sig")
    except OSError as exc:
        raise GuardError(f"无法读取 {path}：{exc}") from exc
    try:
        config = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise GuardError(f"{path} 不是合法 JSON，已停止操作且不修改原文件：{exc}") from exc
    _validate_config(config, path)
    return config


def _write_backup(path: Path) -> Path:
    backup = path.with_name(path.name + BACKUP_SUFFIX)
    atomic_write(backup, path.read_text(encoding="utf-8"))
    return backup


def _write_hooks_config(path: Path, config: dict) -> None:
    atomic_write(path, json.dumps(config, ensure_ascii=False, indent=2) + "\n")


def install_platform_hooks(platform: HookPlatform, root: Path) -> dict:
    """把 Memory Corridor 的 hook 合并进平台配置文件（幂等，保留第三方）。"""
    path = platform.config_path_name
    if not path.is_absolute():
        path = Path(root) / path
    if path.exists():
        config = read_hooks_config(path)
        existed = True
    else:
        config = json.loads(json.dumps(platform.fresh_top_level))  # 深拷贝默认结构
        existed = False
    hooks = config.setdefault("hooks", {})
    added: list[str] = []
    already: list[str] = []
    for event, matcher in platform.matchers.items():
        groups = hooks.setdefault(event, [])
        if _contains_managed(groups, platform):
            already.append(event)
            continue
        group = _group_for(platform, event)
        if matcher:
            group["matcher"] = matcher
        groups.append(group)
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


def uninstall_platform_hooks(platform: HookPlatform, root: Path) -> dict:
    """只移除 Memory Corridor 自己的 hook，第三方 hook 原样保留（幂等）。"""
    path = platform.config_path_name
    if not path.is_absolute():
        path = Path(root) / path
    if not path.exists():
        return {"removed": [], "path": str(path), "written": False, "backup": None, "absent": True}
    config = read_hooks_config(path)
    hooks = config.get("hooks", {})
    removed: set[str] = set()
    for event, groups in list(hooks.items()):
        kept_groups = []
        for group in groups:
            entries = group.get("hooks", [])
            kept = [handler for handler in entries if not _is_managed_handler(handler, platform)]
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
    return {"removed": sorted(removed), "path": str(path), "written": True, "backup": str(backup)}


def platform_hook_status(platform: HookPlatform, root: Path) -> dict:
    """只读检查安装状态。trust 状态无公开接口，一律提示人工确认。"""
    paths = project_paths(root)
    path = platform.config_path_name
    if not path.is_absolute():
        path = paths.root / path
    result: dict = {
        "platform": platform.name,
        "hooks_file": str(path),
        "hooks_file_exists": path.exists(),
        "hooks_file_valid": None,
        "hooks_file_error": None,
        "events": {},
        "project_initialized": paths.state.exists(),
        "command_on_path": _command_on_path(platform.command),
        "trust": "unable to determine automatically",
    }
    if not path.exists():
        return result
    try:
        config = read_hooks_config(path)
    except GuardError as exc:
        result["hooks_file_valid"] = False
        result["hooks_file_error"] = str(exc)
        return result
    result["hooks_file_valid"] = True
    hooks = config.get("hooks", {})
    for event, matcher in platform.matchers.items():
        expected_matcher = matcher
        entry = {
            "configured": False,
            "matcher": None,
            "matcher_expected": expected_matcher,
            "matcher_drifted": False,
            "third_party_present": False,
        }
        for group in hooks.get(event, []):
            handlers = group.get("hooks", [])
            if any(_is_managed_handler(handler, platform) for handler in handlers):
                entry["configured"] = True
                entry["matcher"] = group.get("matcher")
            if any(not _is_managed_handler(handler, platform) for handler in handlers):
                entry["third_party_present"] = True
        if not entry["configured"] and hooks.get(event):
            entry["third_party_present"] = True
        if entry["configured"] and expected_matcher is not None:
            # matcher 被手动改动会导致 hook 触发条件悄然变化，status 需明示。
            entry["matcher_drifted"] = entry["matcher"] != expected_matcher
        result["events"][event] = entry
    return result


def _command_on_path(command: str) -> bool:
    import shutil

    return shutil.which(command.split()[0]) is not None
