from __future__ import annotations

import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

import context_guard_lite
from context_guard_lite.cli import main
from context_guard_lite.contract import GuardError, add_note, init_project, load_state, project_paths, read_events
from context_guard_lite.evidence import add_evidence
from context_guard_lite.gate import check_gate
from context_guard_lite.recovery import build_packet, write_packet
from context_guard_lite.requirements import KINDS, STATUSES, add_requirement, update_requirement


class ContextGuardLiteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        init_project(self.root, "test-project")
        self.paths = project_paths(self.root)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_init_creates_local_notebook_and_json_state(self) -> None:
        state = load_state(self.paths)
        self.assertTrue(state["contract"]["enabled"])
        self.assertTrue(self.paths.notebook.exists())
        self.assertTrue(self.paths.events.exists())
        events = [json.loads(line) for line in self.paths.events.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(events[0]["type"], "contract.init")

    def test_gate_requires_done_and_current_success_evidence(self) -> None:
        requirement = add_requirement(self.paths, "完成核心功能")
        self.assertFalse(check_gate(self.paths)["ok"])

        add_evidence(self.paths, requirement["id"], "功能已完成", "success", target="src/main.py")
        blocked = check_gate(self.paths)
        self.assertFalse(blocked["ok"])
        self.assertIn("不是 done", blocked["blocking"][0]["reasons"][0])

        update_requirement(self.paths, requirement["id"], status="done")
        self.assertTrue(check_gate(self.paths)["ok"])

    def test_requirement_revision_invalidates_old_evidence(self) -> None:
        requirement = add_requirement(self.paths, "输出 JSON")
        add_evidence(self.paths, requirement["id"], "旧版本已验证", "success")
        update_requirement(self.paths, requirement["id"], status="done")
        self.assertTrue(check_gate(self.paths)["ok"])

        update_requirement(self.paths, requirement["id"], text="输出 UTF-8 JSON")
        self.assertFalse(check_gate(self.paths)["ok"])
        add_evidence(self.paths, requirement["id"], "新版本已验证", "success")
        self.assertTrue(check_gate(self.paths)["ok"])

    def test_latest_failed_evidence_blocks_an_earlier_success(self) -> None:
        requirement = add_requirement(self.paths, "运行验证")
        update_requirement(self.paths, requirement["id"], status="done")
        add_evidence(self.paths, requirement["id"], "第一次通过", "success")
        self.assertTrue(check_gate(self.paths)["ok"])
        add_evidence(self.paths, requirement["id"], "第二次失败", "failed")
        self.assertFalse(check_gate(self.paths)["ok"])

    def test_recovery_contains_open_items_and_notebook_tail(self) -> None:
        add_requirement(self.paths, "保留原有 API")
        add_note(self.paths, "先看现有调用方，再改共享函数", kind="experience", source="test")
        packet = build_packet(self.paths)
        self.assertIn("保留原有 API", packet)
        self.assertIn("先看现有调用方", packet)
        self.assertIn("Completion Gate", packet)

    def test_nested_cli_commands_keep_top_level_command(self) -> None:
        self.assertEqual(main(["--root", str(self.root), "requirements", "add", "CLI 可用"]), 0)
        self.assertEqual(main(["--root", str(self.root), "evidence", "add", "--for", "R001", "--summary", "命令通过", "--result", "success"]), 0)
        self.assertEqual(main(["--root", str(self.root), "requirements", "update", "R001", "--status", "done"]), 0)
        self.assertEqual(main(["--root", str(self.root), "gate", "check"]), 0)


class EdgeCaseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.paths = project_paths(self.root)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_init_twice_is_rejected_without_overwrite(self) -> None:
        init_project(self.root, "first")
        with self.assertRaises(GuardError):
            init_project(self.root, "second")
        self.assertEqual(load_state(self.paths)["project"]["name"], "first")

    def test_commands_before_init_fail_cleanly(self) -> None:
        self.assertEqual(main(["--root", str(self.root), "status"]), 2)
        self.assertEqual(main(["--root", str(self.root), "gate", "check"]), 2)

    def test_gate_idle_when_ledger_empty(self) -> None:
        # 空账本是「信息不足」不是「验收失败」：放行（idle），让新用户不被误拦。
        init_project(self.root, "empty")
        result = check_gate(self.paths)
        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], "idle")
        self.assertEqual(result["blocking"], [])

    def test_gate_blocks_when_all_superseded(self) -> None:
        # 曾经有需求、现在全部 superseded：没有可验收目标，仍然阻塞。
        init_project(self.root, "all-superseded")
        add_requirement(self.paths, "被取代的要求")
        update_requirement(self.paths, "R001", status="superseded")
        result = check_gate(self.paths)
        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["blocking"][0]["reason"], "all_superseded")

    def test_gate_check_exit_code_distinguishes_idle_and_blocked(self) -> None:
        # idle/disabled=0（未配置），blocked=1（未通过）：脚本可分辨两种失败。
        init_project(self.root, "exit-codes")
        self.assertEqual(main(["--root", str(self.root), "gate", "check"]), 0)
        add_requirement(self.paths, "未完成的要求")
        self.assertEqual(main(["--root", str(self.root), "gate", "check"]), 1)

    def test_off_disables_gate_but_keeps_records(self) -> None:
        init_project(self.root, "toggled")
        self.assertEqual(main(["--root", str(self.root), "off"]), 0)
        self.assertEqual(check_gate(self.paths)["status"], "disabled")
        state = load_state(self.paths)
        self.assertFalse(state["contract"]["enabled"])
        self.assertEqual(main(["--root", str(self.root), "on"]), 0)
        self.assertEqual(check_gate(self.paths)["status"], "idle")

    def test_unknown_evidence_result_is_rejected(self) -> None:
        init_project(self.root, "evidence-check")
        add_requirement(self.paths, "要求一")
        with self.assertRaises(GuardError):
            add_evidence(self.paths, "R001", "摘要", "maybe")

    def test_empty_note_and_empty_update_are_rejected(self) -> None:
        init_project(self.root, "input-check")
        add_requirement(self.paths, "要求一")
        with self.assertRaises(GuardError):
            add_note(self.paths, "   ")
        with self.assertRaises(GuardError):
            update_requirement(self.paths, "R001")

    def test_recovery_packet_written_to_custom_relative_path(self) -> None:
        init_project(self.root, "packet-path")
        add_requirement(self.paths, "要求一")
        target = write_packet(self.paths, out="exports/recovery.md")
        # project_paths 会把 root resolve 成真实路径（macOS 上 /var → /private/var），所以以 paths.root 为基准比较。
        self.assertEqual(target, self.paths.root / "exports" / "recovery.md")
        self.assertTrue(target.exists())
        self.assertIn("要求一", target.read_text(encoding="utf-8"))

    def test_version_consistency_and_cli_version_flag(self) -> None:
        import tomllib

        pyproject = Path(__file__).resolve().parent.parent / "pyproject.toml"
        with pyproject.open("rb") as handle:
            declared = tomllib.load(handle)["project"]["version"]
        self.assertEqual(context_guard_lite.__version__, declared)
        with self.assertRaises(SystemExit) as ctx:
            main(["--version"])
        self.assertEqual(ctx.exception.code, 0)

    def test_kind_and_status_values_unchanged(self) -> None:
        self.assertEqual(KINDS, {"must", "avoid", "acceptance"})
        self.assertEqual(STATUSES, {"open", "blocked", "done", "superseded"})


class RecoveryPacketTests(unittest.TestCase):
    """恢复包分层：已验证完成项折叠，未满足项（含证据不合格的 done）保持全列。"""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        init_project(self.root, "packet-fold")
        self.paths = project_paths(self.root)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_recovery_packet_folds_completed_requirements(self) -> None:
        for index in range(1, 26):
            requirement = add_requirement(self.paths, f"已完成要求 {index:02d}")
            add_evidence(self.paths, requirement["id"], f"证据 {index:02d}", "success")
            update_requirement(self.paths, requirement["id"], status="done")
        pending_one = add_requirement(self.paths, "待办甲")
        pending_two = add_requirement(self.paths, "待办乙")

        packet = build_packet(self.paths)
        self.assertIn("待办甲", packet)
        self.assertIn("待办乙", packet)
        self.assertIn("25 项已验证，列出最近 20 条", packet)
        self.assertIn("另有 5 项见 state.json", packet)
        # 最早的已完成项被折叠，最近 20 条保留
        self.assertNotIn("已完成要求 01\n", packet)
        self.assertNotIn("已完成要求 05", packet)
        self.assertIn("已完成要求 25", packet)
        # satisfied 项不得混入待办区
        pending_section = packet.split("## 已完成")[0]
        self.assertNotIn("已完成要求 06", pending_section)
        gate = check_gate(self.paths)
        self.assertEqual(
            [item["requirement_id"] for item in gate["blocking"]],
            [pending_one["id"], pending_two["id"]],
        )

    def test_recovery_packet_keeps_unsatisfied_done_visible(self) -> None:
        done_no_evidence = add_requirement(self.paths, "完成但无证据")
        update_requirement(self.paths, done_no_evidence["id"], status="done")
        done_failed = add_requirement(self.paths, "完成但最新失败")
        add_evidence(self.paths, done_failed["id"], "失败的验证", "failed")
        update_requirement(self.paths, done_failed["id"], status="done")
        satisfied = add_requirement(self.paths, "完全满足项")
        add_evidence(self.paths, satisfied["id"], "通过的验证", "success")
        update_requirement(self.paths, satisfied["id"], status="done")

        packet = build_packet(self.paths)
        pending_section = packet.split("## 已完成")[0]
        self.assertIn("完成但无证据", pending_section)
        self.assertIn("完成但最新失败", pending_section)
        self.assertNotIn("完全满足项", pending_section)
        self.assertFalse(check_gate(self.paths)["ok"])


class LedgerQueryTests(unittest.TestCase):
    """账本查询增强（list 过滤、events 审计）：每个用例使用独立已初始化项目。"""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        init_project(self.root, "query-project")
        self.paths = project_paths(self.root)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _list_output(self, argv: list[str]) -> str:
        import contextlib as _contextlib
        import io as _io

        with _contextlib.redirect_stdout(_io.StringIO()) as fake:
            self.assertEqual(main(argv), 0)
        return fake.getvalue()

    def test_requirements_list_filters_by_kind_and_status(self) -> None:
        r1 = add_requirement(self.paths, "must 类要求")
        add_requirement(self.paths, "avoid 类要求", kind="avoid")
        update_requirement(self.paths, r1["id"], status="done")

        by_kind = self._list_output(["--root", str(self.root), "requirements", "list", "--kind", "must"])
        self.assertIn("must 类要求", by_kind)
        self.assertNotIn("avoid 类要求", by_kind)

        by_status = self._list_output(["--root", str(self.root), "requirements", "list", "--status", "open"])
        self.assertIn("avoid 类要求", by_status)
        self.assertNotIn("must 类要求", by_status)

        everything = self._list_output(["--root", str(self.root), "requirements", "list"])
        self.assertIn("must 类要求", everything)
        self.assertIn("avoid 类要求", everything)

    def test_evidence_list_filters_by_requirement(self) -> None:
        r1 = add_requirement(self.paths, "要求一")
        r2 = add_requirement(self.paths, "要求二")
        add_evidence(self.paths, r1["id"], "要求一的证据", "success")
        add_evidence(self.paths, r2["id"], "要求二的证据", "failed")

        filtered = self._list_output(["--root", str(self.root), "evidence", "list", "--for", r1["id"]])
        self.assertIn("要求一的证据", filtered)
        self.assertNotIn("要求二的证据", filtered)

        all_rows = self._list_output(["--root", str(self.root), "evidence", "list"])
        self.assertIn("要求一的证据", all_rows)
        self.assertIn("要求二的证据", all_rows)

    def test_note_list_filters_by_kind_and_source(self) -> None:
        from context_guard_lite.contract import add_note

        add_note(self.paths, "经验条目", kind="experience", source="ai")
        add_note(self.paths, "决定条目", kind="decision", source="user")
        add_note(self.paths, "AI 决定条目", kind="decision", source="ai")

        by_kind = self._list_output(["--root", str(self.root), "note", "list", "--kind", "experience"])
        self.assertIn("经验条目", by_kind)
        self.assertNotIn("决定条目", by_kind)

        by_source = self._list_output(["--root", str(self.root), "note", "list", "--source", "ai"])
        self.assertIn("经验条目", by_source)
        self.assertIn("AI 决定条目", by_source)
        matched = [line for line in by_source.splitlines() if "决定条目" in line]
        self.assertEqual(len(matched), 1)

    def test_events_list_shows_recent_and_filters_by_type(self) -> None:
        requirement = add_requirement(self.paths, "事件审计要求")
        add_evidence(self.paths, requirement["id"], "事件审计证据", "success")

        recent = self._list_output(["--root", str(self.root), "events", "list", "--limit", "3"])
        self.assertIn("evidence.add", recent)

        only_req = self._list_output(["--root", str(self.root), "events", "list", "--type", "requirement.add"])
        self.assertIn("requirement.add", only_req)
        self.assertNotIn("evidence.add", only_req)

        empty = self._list_output(["--root", str(self.root), "events", "list", "--limit", "0"])
        self.assertIn("暂无匹配的事件", empty)

    def test_events_list_requires_initialized_project(self) -> None:
        import contextlib as _contextlib
        import io as _io

        bare = Path(self.temp_dir.name) / "bare"
        bare.mkdir()
        with _contextlib.redirect_stderr(_io.StringIO()):
            self.assertEqual(main(["--root", str(bare), "events", "list"]), 2)

    def test_gate_check_text_output_caps_and_all_flag(self) -> None:
        for index in range(1, 26):
            add_requirement(self.paths, f"阻塞要求 {index:02d}")
        import contextlib as _contextlib
        import io as _io

        with _contextlib.redirect_stdout(_io.StringIO()) as capped:
            self.assertEqual(main(["--root", str(self.root), "gate", "check"]), 1)
        self.assertIn("阻塞要求 20", capped.getvalue())
        self.assertNotIn("阻塞要求 21", capped.getvalue())
        self.assertIn("另有 5 个阻塞项未显示", capped.getvalue())

        with _contextlib.redirect_stdout(_io.StringIO()) as full:
            self.assertEqual(main(["--root", str(self.root), "gate", "check", "--all"]), 1)
        self.assertIn("阻塞要求 25", full.getvalue())
        self.assertNotIn("未显示", full.getvalue())

        # --json 保持全量供程序读取
        with _contextlib.redirect_stdout(_io.StringIO()) as as_json:
            self.assertEqual(main(["--root", str(self.root), "gate", "check", "--json"]), 1)
        parsed = json.loads(as_json.getvalue())
        self.assertEqual(len(parsed["blocking"]), 25)

    def test_status_reports_recovery_packet_freshness(self) -> None:
        import contextlib as _contextlib
        import io as _io

        with _contextlib.redirect_stdout(_io.StringIO()) as before:
            self.assertEqual(main(["--root", str(self.root), "status"]), 0)
        self.assertIn("未生成", before.getvalue())

        from context_guard_lite.recovery import write_packet

        write_packet(self.paths)
        with _contextlib.redirect_stdout(_io.StringIO()) as after:
            self.assertEqual(main(["--root", str(self.root), "status"]), 0)
        self.assertIn("恢复包：已生成", after.getvalue())

        with _contextlib.redirect_stdout(_io.StringIO()) as as_json:
            self.assertEqual(main(["--root", str(self.root), "status", "--json"]), 0)
        parsed = json.loads(as_json.getvalue())
        self.assertTrue(parsed["recovery_generated_at"].endswith("Z"))

    def test_requirements_done_shortcut_warns_without_evidence(self) -> None:
        import contextlib as _contextlib
        import io as _io

        requirement = add_requirement(self.paths, "完成但忘了证据")
        with _contextlib.redirect_stdout(_io.StringIO()) as out:
            self.assertEqual(main(["--root", str(self.root), "requirements", "done", requirement["id"]]), 0)
        self.assertIn("[done]", out.getvalue())
        self.assertIn("警告", out.getvalue())
        self.assertIn("evidence add --for R001", out.getvalue())
        self.assertFalse(check_gate(self.paths)["ok"])

        add_evidence(self.paths, requirement["id"], "补上真实验证", "success")
        # 已是 done；再次执行 done 糖应提示满足门禁
        with _contextlib.redirect_stdout(_io.StringIO()) as out2:
            self.assertEqual(main(["--root", str(self.root), "requirements", "done", requirement["id"]]), 0)
        self.assertIn("已满足完成门禁条件", out2.getvalue())
        self.assertTrue(check_gate(self.paths)["ok"])

    def test_requirements_done_after_revision_bump_requires_new_evidence(self) -> None:
        import contextlib as _contextlib
        import io as _io

        requirement = add_requirement(self.paths, "口径 v1")
        add_evidence(self.paths, requirement["id"], "v1 验证通过", "success")
        with _contextlib.redirect_stdout(_io.StringIO()) as first:
            self.assertEqual(main(["--root", str(self.root), "requirements", "done", requirement["id"]]), 0)
        self.assertIn("已满足完成门禁条件", first.getvalue())

        update_requirement(self.paths, requirement["id"], text="口径 v2（旧证据失效）")
        with _contextlib.redirect_stdout(_io.StringIO()) as second:
            self.assertEqual(main(["--root", str(self.root), "requirements", "done", requirement["id"]]), 0)
        self.assertIn("警告", second.getvalue())
        self.assertIn("v2", second.getvalue())

    def test_import_requirements_from_lines(self) -> None:
        from context_guard_lite.requirements import import_requirements

        lines = [
            "第一条要求",
            "   ",
            "# 这是注释行",
            "  第二条要求（前导空白会被折叠）  ",
        ]
        result = import_requirements(self.paths, lines, kind="must")
        self.assertEqual(len(result["imported"]), 2)
        self.assertEqual(result["skipped"], 2)
        self.assertEqual([item["id"] for item in result["imported"]], ["R001", "R002"])

        events = read_events(self.paths, event_type="requirement.add")
        imported_flags = [event.get("imported") for event in events if event.get("id") in {"R001", "R002"}]
        self.assertEqual(imported_flags, [True, True])

    def test_import_cli_from_file_with_bom(self) -> None:
        import contextlib as _contextlib
        import io as _io

        source = self.root / "tasks.txt"
        source.write_bytes(b"\xef\xbb\xbf" + "第一条导入\n\n# 注释\n第二条导入\n".encode())
        with _contextlib.redirect_stdout(_io.StringIO()) as out:
            self.assertEqual(main(["--root", str(self.root), "requirements", "import", str(source)]), 0)
        self.assertIn("导入 2 条，跳过 2 行", out.getvalue())
        items = load_state(self.paths)["requirements"]
        self.assertEqual([item["text"] for item in items], ["第一条导入", "第二条导入"])

    def test_import_cli_stdin_and_missing_file(self) -> None:
        import contextlib as _contextlib
        import io as _io

        original_stdin = sys.stdin
        sys.stdin = io.BytesIO("来自 stdin 的要求\n".encode())
        try:
            with _contextlib.redirect_stdout(_io.StringIO()):
                self.assertEqual(main(["--root", str(self.root), "requirements", "import", "-"]), 0)
        finally:
            sys.stdin = original_stdin
        self.assertEqual(load_state(self.paths)["requirements"][0]["text"], "来自 stdin 的要求")

        with _contextlib.redirect_stderr(_io.StringIO()):
            self.assertEqual(main(["--root", str(self.root), "requirements", "import", "no-such-file.txt"]), 2)

    def test_recovery_packet_max_done_flag(self) -> None:
        import contextlib as _contextlib
        import io as _io

        for index in range(1, 6):
            requirement = add_requirement(self.paths, f"完成项 {index}")
            add_evidence(self.paths, requirement["id"], "通过", "success")
            update_requirement(self.paths, requirement["id"], status="done")

        with _contextlib.redirect_stdout(_io.StringIO()) as zero:
            self.assertEqual(main(["--root", str(self.root), "recovery", "packet", "--max-done", "0"]), 0)
        self.assertIn("列出最近 0 条", zero.getvalue())
        completed_zero = zero.getvalue().split("## 已完成")[1].split("## 最近 evidence")[0]
        self.assertNotIn("完成项 5", completed_zero)

        with _contextlib.redirect_stdout(_io.StringIO()) as three:
            self.assertEqual(main(["--root", str(self.root), "recovery", "packet", "--max-done", "3"]), 0)
        completed_three = three.getvalue().split("## 已完成")[1].split("## 最近 evidence")[0]
        self.assertEqual(completed_three.count("[x]"), 3)


if __name__ == "__main__":
    unittest.main()
