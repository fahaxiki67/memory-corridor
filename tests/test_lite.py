from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from context_guard_lite.contract import init_project, load_state, project_paths
from context_guard_lite.cli import main
from context_guard_lite.evidence import add_evidence
from context_guard_lite.gate import check_gate
from context_guard_lite.recovery import build_packet
from context_guard_lite.requirements import add_requirement, update_requirement


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
        from context_guard_lite.contract import add_note

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


if __name__ == "__main__":
    unittest.main()
