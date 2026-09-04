from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .contract import GuardError, add_note, init_project, load_state, project_paths, set_enabled
from .evidence import add_evidence, list_evidence
from .gate import check_gate
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

    note = commands.add_parser("note", aliases=["notebook"], help="向旁记事本追加记录")
    note_commands = note.add_subparsers(dest="note_command", required=True)
    note_add = note_commands.add_parser("add", help="新增经验、决定或普通笔记")
    note_add.add_argument("text")
    note_add.add_argument("--kind", choices=sorted(NOTE_KINDS), default="experience")
    note_add.add_argument("--source", default="manual")
    note_list = note_commands.add_parser("list", help="列出结构化笔记")
    note_list.add_argument("--limit", type=int, default=20)

    return parser


def _print_json(value: object) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2))


def _cmd_status(paths, as_json: bool) -> int:
    state = load_state(paths)
    gate = check_gate(paths)
    result = {
        "project": state["project"],
        "enabled": state["contract"].get("enabled", False),
        "requirements": {"total": len(state["requirements"]), "active": sum(item.get("status") != "superseded" for item in state["requirements"])},
        "evidence": len(state["evidence"]),
        "notes": len(state["notes"]),
        "gate": gate,
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
        print(f"旁记事本：{paths.notebook}")
    return 0


def _cmd_requirements(paths, args) -> int:
    if args.requirements_command == "add":
        requirement = add_requirement(paths, args.text, args.kind)
        print(f"已新增 {requirement['id']} v{requirement['revision']}：{requirement['text']}")
        return 0
    if args.requirements_command == "list":
        items = load_state(paths)["requirements"]
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
        for item in result["blocking"]:
            if "requirement_id" in item:
                print(f"- {item['requirement_id']}：{item['text']}")
                for reason in item["reasons"]:
                    print(f"  - {reason}")
            else:
                print(f"- {item.get('reason')}")
    return 0 if result["ok"] else 1


def _cmd_note(paths, args) -> int:
    if args.note_command == "add":
        note = add_note(paths, args.text, args.kind, args.source)
        print(f"已记录 {note['id']} [{note['kind']}]：{note['text']}")
        return 0
    notes = load_state(paths)["notes"][-args.limit :] if args.limit > 0 else []
    if not notes:
        print("暂无结构化笔记。")
    else:
        for note in notes:
            print(f"{note['id']} [{note['kind']}] [{note['source']}]：{note['text']}")
    return 0


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
        parser.error(f"未知命令: {args.top_command}")
    except (GuardError, OSError, ValueError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2
    return 2
