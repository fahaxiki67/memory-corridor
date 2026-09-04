from __future__ import annotations

import argparse
import json
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

from .contract import GuardError, add_note, init_project, load_state, project_paths, read_events, set_enabled
from .evidence import add_evidence, list_evidence
from .gate import check_gate
from .integrations import codex as codex_integration
from .recovery import build_packet, write_packet
from .requirements import KINDS, STATUSES, add_requirement, update_requirement

from . import __version__


NOTE_KINDS = {"experience", "decision", "lesson", "note"}


def _configure_output() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure:
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (OSError, ValueError):
                pass


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="memory-corridor",
        description="记忆回廊（Context Guard Lite 2.0）：本地任务账本、旁记事本与完成门禁。",
    )
    parser.add_argument("--root", type=Path, default=Path.cwd(), help="项目目录，默认当前目录")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    commands = parser.add_subparsers(dest="top_command", required=True)

    init = commands.add_parser("init", help="初始化项目账本")
    init.add_argument("--name", help="项目显示名称")

    commands.add_parser("on", help="开启保护")
    commands.add_parser("off", help="关闭保护但保留记录")

    status = commands.add_parser("status", help="查看状态")
    status.add_argument("--json", action="store_true", dest="as_json")

    requirements = commands.add_parser("requirements", aliases=["req"], help="管理 requirements")
    requirement_commands = requirements.add_subparsers(dest="requirements_command", required=True)
    req_add = requirement_commands.add_parser("add", help="新增 requirement")
    req_add.add_argument("text")
    req_add.add_argument("--kind", choices=sorted(KINDS), default="must")
    req_list = requirement_commands.add_parser("list", help="列出 requirements")
    req_list.add_argument("--json", action="store_true", dest="as_json")
    req_list.add_argument("--kind", help="只列出指定类型的 requirements")
    req_list.add_argument("--status", help="只列出指定状态的 requirements")
    req_update = requirement_commands.add_parser("update", help="更新 requirement")
    req_update.add_argument("requirement_id")
    req_update.add_argument("--text")
    req_update.add_argument("--kind", choices=sorted(KINDS))
    req_update.add_argument("--status", choices=sorted(STATUSES))
    req_update.add_argument("--reason")

    evidence = commands.add_parser("evidence", help="记录 evidence")
    evidence_commands = evidence.add_subparsers(dest="evidence_command", required=True)
    evidence_add = evidence_commands.add_parser("add", help="新增 evidence")
    evidence_add.add_argument("--for", dest="requirement_id", required=True, help="绑定的 requirement ID")
    evidence_add.add_argument("--summary", required=True, help="简短的验证摘要")
    evidence_add.add_argument("--result", choices=["success", "failed", "unknown"], required=True)
    evidence_add.add_argument("--target", help="验证的文件、URL 或其他目标")
    evidence_add.add_argument("--command", help="实际执行的验证命令")
    evidence_list = evidence_commands.add_parser("list", help="列出 evidence")
    evidence_list.add_argument("--json", action="store_true", dest="as_json")
    evidence_list.add_argument("--for", dest="filter_requirement", help="只列出绑定到指定 requirement ID 的 evidence")

    recovery = commands.add_parser("recovery", help="生成恢复包")
    recovery_commands = recovery.add_subparsers(dest="recovery_command", required=True)
    recovery_packet = recovery_commands.add_parser("packet", help="生成并保存 recovery.md")
    recovery_packet.add_argument("--out", type=Path, help="输出文件，默认 .context-guard/recovery.md")
    recovery_packet.add_argument("--max-evidence", type=int, default=10)
    recovery_packet.add_argument("--notebook-lines", type=int, default=20)

    gate = commands.add_parser("gate", help="检查完成门禁")
    gate_commands = gate.add_subparsers(dest="gate_command", required=True)
    gate_check = gate_commands.add_parser("check", help="检查是否允许报告完成")
    gate_check.add_argument("--json", action="store_true", dest="as_json")
    gate_check.add_argument("--all", action="store_true", dest="show_all", help="列出全部阻塞项（默认只列前 20 条）")

    note = commands.add_parser("note", aliases=["notebook"], help="向旁记事本追加记录")
    note_commands = note.add_subparsers(dest="note_command", required=True)
    note_add = note_commands.add_parser("add", help="新增经验、决定或普通笔记")
    note_add.add_argument("text")
    note_add.add_argument("--kind", choices=sorted(NOTE_KINDS), default="experience")
    note_add.add_argument("--source", default="manual")
    note_list = note_commands.add_parser("list", help="列出结构化笔记")
    note_list.add_argument("--limit", type=int, default=20)
    note_list.add_argument("--kind", help="只列出指定类型的笔记")
    note_list.add_argument("--source", help="只列出指定来源的笔记")

    events = commands.add_parser("events", help="查看事件日志（只读审计 events.jsonl）")
    events_commands = events.add_subparsers(dest="events_command", required=True)
    ev_list = events_commands.add_parser("list", help="列出事件")
    ev_list.add_argument("--limit", type=int, default=20, help="只显示最近 N 条（0=不显示）")
    ev_list.add_argument("--type", dest="event_type", help="按事件类型过滤，如 requirement.add")

    codex = commands.add_parser("codex", help="Codex 原生 Hook 集成")
    codex_commands = codex.add_subparsers(dest="codex_command", required=True)
    codex_commands.add_parser("hook", help="处理 Codex Hook 事件（stdin JSON 进，stdout JSON 出）")
    codex_commands.add_parser("install", help="把三个 Memory Corridor Hook 合并进 <项目>/.codex/hooks.json")
    codex_status = codex_commands.add_parser("status", help="检查 Codex Hook 安装状态")
    codex_status.add_argument("--json", action="store_true", dest="as_json")
    codex_commands.add_parser("uninstall", help="只移除 Memory Corridor Hook，保留第三方 Hook")

    return parser


def _print_json(value: object) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2))


def _cmd_status(paths, as_json: bool) -> int:
    state = load_state(paths)
    gate = check_gate(paths)
    recovery_generated_at = None
    if paths.recovery.exists():
        recovery_generated_at = datetime.fromtimestamp(
            paths.recovery.stat().st_mtime, tz=timezone.utc
        ).isoformat(timespec="seconds").replace("+00:00", "Z")
    result = {
        "project": state["project"],
        "enabled": state["contract"].get("enabled", False),
        "requirements": {"total": len(state["requirements"]), "active": sum(item.get("status") != "superseded" for item in state["requirements"])},
        "evidence": len(state["evidence"]),
        "notes": len(state["notes"]),
        "gate": gate,
        "recovery_generated_at": recovery_generated_at,
        "files": {
            "state": str(paths.state),
            "notebook": str(paths.notebook),
            "recovery": str(paths.recovery),
        },
    }
    if as_json:
        _print_json(result)
    else:
        print(f"项目：{state['project']['name']}")
        print(f"保护：{'开启' if result['enabled'] else '关闭'}")
        print(f"Requirements：{result['requirements']['active']} active / {result['requirements']['total']} total")
        print(f"Evidence：{result['evidence']}；笔记：{result['notes']}")
        print(f"Gate：{gate['status']} — {gate['summary']}")
        if recovery_generated_at:
            print(f"恢复包：已生成（{recovery_generated_at}）")
        else:
            print("恢复包：未生成（运行 recovery packet，或等待 PreCompact 自动刷新）")
        print(f"旁记事本：{paths.notebook}")
    return 0


def _cmd_requirements(paths, args) -> int:
    if args.requirements_command == "add":
        requirement = add_requirement(paths, args.text, args.kind)
        print(f"已新增 {requirement['id']} v{requirement['revision']}：{requirement['text']}")
        return 0
    if args.requirements_command == "list":
        items = load_state(paths)["requirements"]
        if getattr(args, "kind", None):
            items = [item for item in items if item.get("kind") == args.kind]
        if getattr(args, "status", None):
            items = [item for item in items if item.get("status") == args.status]
        if args.as_json:
            _print_json(items)
        elif not items:
            print("暂无 requirements。")
        else:
            for item in items:
                print(f"{item['id']} [{item['status']}] [{item['kind']}] v{item['revision']}：{item['text']}")
        return 0
    requirement = update_requirement(
        paths,
        args.requirement_id,
        text=args.text,
        kind=args.kind,
        status=args.status,
        reason=args.reason,
    )
    print(f"已更新 {requirement['id']}：[{requirement['status']}] v{requirement['revision']} {requirement['text']}")
    return 0


def _cmd_evidence(paths, args) -> int:
    if args.evidence_command == "add":
        evidence = add_evidence(
            paths,
            args.requirement_id,
            args.summary,
            args.result,
            target=args.target,
            command=args.command,
        )
        print(f"已记录 {evidence['id']} → {evidence['requirement_id']} v{evidence['requirement_revision']} [{evidence['result']}]")
        return 0
    items = list_evidence(paths)
    filter_requirement = getattr(args, "filter_requirement", None)
    if filter_requirement:
        target = filter_requirement.upper()
        items = [item for item in items if item.get("requirement_id", "").upper() == target]
    if args.as_json:
        _print_json(items)
    elif not items:
        print("暂无 evidence。")
    else:
        for item in items:
            print(f"{item['id']} → {item['requirement_id']} v{item['requirement_revision']} [{item['result']}]：{item['summary']}")
    return 0


def _cmd_recovery(paths, args) -> int:
    packet = build_packet(paths, max_evidence=args.max_evidence, notebook_lines=args.notebook_lines)
    target = write_packet(
        paths,
        out=args.out,
        content=packet,
    )
    print(packet, end="")
    print(f"\n已保存：{target}")
    return 0


def _cmd_gate(paths, args) -> int:
    result = check_gate(paths)
    if args.as_json:
        _print_json(result)
    else:
        label = {"pass": "PASS", "blocked": "BLOCKED", "disabled": "DISABLED"}[result["status"]]
        print(f"{label}：{result['summary']}")
        blocking = result["blocking"]
        shown = blocking if args.show_all else blocking[:20]
        for item in shown:
            if "requirement_id" in item:
                print(f"- {item['requirement_id']}：{item['text']}")
                for reason in item["reasons"]:
                    print(f"  - {reason}")
            else:
                print(f"- {item.get('reason')}")
        hidden = len(blocking) - len(shown)
        if hidden > 0:
            print(f"… 另有 {hidden} 个阻塞项未显示（--all 查看全部；--json 供程序读取）")
    return 0 if result["ok"] else 1


def _cmd_note(paths, args) -> int:
    if args.note_command == "add":
        note = add_note(paths, args.text, args.kind, args.source)
        print(f"已记录 {note['id']} [{note['kind']}]：{note['text']}")
        return 0
    notes = load_state(paths)["notes"]
    if getattr(args, "kind", None):
        notes = [note for note in notes if note.get("kind") == args.kind]
    if getattr(args, "source", None):
        notes = [note for note in notes if note.get("source") == args.source]
    notes = notes[-args.limit :] if args.limit > 0 else []
    if not notes:
        print("暂无结构化笔记。")
    else:
        for note in notes:
            print(f"{note['id']} [{note['kind']}] [{note['source']}]：{note['text']}")
    return 0


def _print_codex_trust_reminder() -> None:
    print("注意：Codex 对项目级非托管 Hook 要求人工 review/trust，信任之前不会运行：")
    print("1. 在项目目录启动 codex；")
    print("2. 运行 /hooks 查看；")
    print("3. 确认 Memory Corridor 三个 Hook（PreCompact / SessionStart / Stop）已识别并信任。")


def _warn_if_command_missing() -> None:
    if shutil.which("memory-corridor") is None:
        print("警告：当前 PATH 上找不到 memory-corridor 命令，Codex 将无法调用 Hook（触发时会报 command not found）。")
        print("请先 pip install 本项目，或在已激活对应 venv 的终端里启动 codex。")


def _print_codex_install(result: dict) -> int:
    if not result["written"]:
        print(f"Codex Hook 已安装，本次未做任何修改：{result['path']}")
        print(f"已存在：{', '.join(result['already'])}")
        _warn_if_command_missing()
        _print_codex_trust_reminder()
        return 0
    print(f"已写入：{result['path']}")
    if result["backup"]:
        print(f"上一版本备份：{result['backup']}")
    for event in result["added"]:
        print(f"- 已添加 {event}")
    if result["already"]:
        print(f"- 已存在，未重复添加：{', '.join(result['already'])}")
    _warn_if_command_missing()
    _print_codex_trust_reminder()
    return 0


def _print_codex_status(result: dict) -> int:
    exists = "存在" if result["hooks_file_exists"] else "不存在"
    print(f"Codex hooks 配置：{result['hooks_file']}（{exists}）")
    if result["hooks_file_valid"] is False:
        print(f"文件无法解析：{result['hooks_file_error']}")
    for event in ("PreCompact", "SessionStart", "Stop"):
        entry = result["events"].get(event)
        if entry is None:
            print(f"- {event}：文件不可读，无法确认")
        elif entry["configured"]:
            matcher = f"，matcher={entry['matcher']}" if entry["matcher"] else ""
            suffix = "（同组含第三方 Hook）" if entry["third_party_present"] else ""
            if entry.get("matcher_drifted"):
                suffix += f"【警告：matcher 已偏离安装值，预期 {entry.get('matcher_expected')}】"
            print(f"- {event}：已安装{matcher}{suffix}")
        elif entry["third_party_present"]:
            print(f"- {event}：仅第三方 Hook，Memory Corridor 未安装")
        else:
            print(f"- {event}：未安装")
    if result["project_initialized"]:
        print("Memory Corridor 项目：已初始化")
    else:
        print("Memory Corridor 项目：未初始化（Hook 将按约定 no-op，不影响 Codex）")
    if result["command_on_path"]:
        print("memory-corridor 命令：PATH 上可用")
    else:
        print("memory-corridor 命令：PATH 上找不到（Codex 将无法调用 Hook，请先 pip install 本项目或激活对应 venv）")
    print("Hook trust：unable to determine automatically（请在 Codex 中运行 /hooks 人工确认）")
    return 0


def _print_codex_uninstall(result: dict) -> int:
    if result.get("absent"):
        print(f"{result['path']} 不存在，没有可卸载的 Memory Corridor Hook。")
        return 0
    if not result["written"]:
        print("未发现 Memory Corridor Hook，未做任何修改。")
        return 0
    print(f"已更新：{result['path']}")
    print(f"已移除：{', '.join(result['removed'])}")
    if result["backup"]:
        print(f"上一版本备份：{result['backup']}")
    print("第三方 Hook 已保留。")
    return 0


def _cmd_events(paths, args) -> int:
    if args.events_command == "list":
        events = read_events(paths, limit=args.limit, event_type=args.event_type)
        if not events:
            print("暂无匹配的事件。")
            return 0
        for event in events:
            details = ", ".join(f"{key}={value}" for key, value in event.items() if key not in {"at", "type"})
            suffix = f"  ({details})" if details else ""
            print(f"{event.get('at', '?')}  {event.get('type', '?')}{suffix}")
        return 0
    return 2


def _cmd_codex(paths, args) -> int:
    if args.codex_command == "hook":
        return codex_integration.run_hook_command()
    if args.codex_command == "install":
        return _print_codex_install(codex_integration.install_hooks(paths.root))
    if args.codex_command == "status":
        result = codex_integration.hook_status(paths.root)
        if args.as_json:
            _print_json(result)
            return 0
        return _print_codex_status(result)
    if args.codex_command == "uninstall":
        return _print_codex_uninstall(codex_integration.uninstall_hooks(paths.root))
    return 2


def main(argv: list[str] | None = None) -> int:
    _configure_output()
    parser = _parser()
    args = parser.parse_args(argv)
    paths = project_paths(args.root)
    try:
        if args.top_command == "init":
            state = init_project(paths.root, args.name)
            print(f"已初始化：{paths.data}")
            print(f"项目：{state['project']['name']}；保护：开启")
            return 0
        if args.top_command == "on":
            set_enabled(paths, True)
            print("保护已开启。")
            return 0
        if args.top_command == "off":
            set_enabled(paths, False)
            print("保护已关闭，记录仍保留。")
            return 0
        if args.top_command == "status":
            return _cmd_status(paths, args.as_json)
        if args.top_command in {"requirements", "req"}:
            return _cmd_requirements(paths, args)
        if args.top_command == "evidence":
            return _cmd_evidence(paths, args)
        if args.top_command == "recovery":
            return _cmd_recovery(paths, args)
        if args.top_command == "gate":
            return _cmd_gate(paths, args)
        if args.top_command in {"note", "notebook"}:
            return _cmd_note(paths, args)
        if args.top_command == "events":
            return _cmd_events(paths, args)
        if args.top_command == "codex":
            return _cmd_codex(paths, args)
        parser.error(f"未知命令: {args.top_command}")
    except (GuardError, OSError, ValueError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2
    return 2
