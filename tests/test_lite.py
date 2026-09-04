from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from context_guard_lite.contract import GuardError, add_note, init_project, load_state, project_paths
from context_guard_lite.cli import main
from context_guard_lite.evidence import add_evidence
from context_guard_lite.gate import check_gate
from context_guard_lite.recovery import build_packet, write_packet
from context_guard_lite.requirements import KINDS, STATUSES, add_requirement, update_requirement

import context_guard_lite


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

    def test_gate_blocks_when_no_requirements(self) -> None:
        init_project(self.root, "empty")
        result = check_gate(self.paths)
        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["blocking"][0]["reason"], "no_requirements")

    def test_off_disables_gate_but_keeps_records(self) -> None:
        init_project(self.root, "toggled")
        self.assertEqual(main(["--root", str(self.root), "off"]), 0)
        self.assertEqual(check_gate(self.paths)["status"], "disabled")
        state = load_state(self.paths)
        self.assertFalse(state["contract"]["enabled"])
        self.assertEqual(main(["--root", str(self.root), "on"]), 0)
        self.assertEqual(check_gate(self.paths)["status"], "blocked")

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
        self.assertEqual(context_guard_lite.__version__, "2.1.0")
        with self.assertRaises(SystemExit) as ctx:
            main(["--version"])
        self.assertEqual(ctx.exception.code, 0)

    def test_kind_and_status_values_unchanged(self) -> None:
        self.assertEqual(KINDS, {"must", "avoid", "acceptance"})
        self.assertEqual(STATUSES, {"open", "blocked", "done", "superseded"})


if __name__ == "__main__":
    unittest.main()
