"""MC-01 回归：损坏的 state.json 必须被拒绝，不能被门禁静默放行。

预审实证（2026-09-26）：``contract={}`` 能通过 ``load_state``，而
``gate.check_gate`` 把缺失的 ``enabled`` 当 disabled，直接返回 ``ok=True``——
损坏状态反而「通过」了完成门禁。本文件锁定修复后的行为：

- 读取侧：结构损坏（缺字段/类型错/词表非法/ID 重复/证据绑定断裂）一律
  GuardError，且不修改原文件、不重建空账本、不自动关闭保护；
- 写入侧：``save_state`` 拒绝把不完整状态写回磁盘；
- 语义保留：用户明确 ``enabled=false``（disabled 放行）与空账本（idle 放行）
  的既有设计不变，正例防止修复把正常资料误伤。
"""

from __future__ import annotations

import copy
import json
import tempfile
import unittest

from context_guard_lite.contract import (
    GuardError,
    init_project,
    load_state,
    project_paths,
    save_state,
)
from context_guard_lite.evidence import add_evidence
from context_guard_lite.gate import check_gate
from context_guard_lite.requirements import add_requirement

VALID_CONTRACT = {"enabled": True, "created_at": "2026-09-26T00:00:00Z", "updated_at": "2026-09-26T00:00:00Z"}


def _requirement(requirement_id: str = "R001", **overrides) -> dict:
    requirement = {
        "id": requirement_id,
        "kind": "must",
        "text": "被验证的要求",
        "status": "open",
        "revision": 1,
        "history": [],
        "created_at": "2026-09-26T00:00:00Z",
        "updated_at": "2026-09-26T00:00:00Z",
    }
    requirement.update(overrides)
    return requirement


class StateIntegrityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = self.temp_dir.name
        init_project(self.root, "integrity")
        self.paths = project_paths(self.root)
        # 预置一条合法 requirement，让证据绑定类损坏有真实的绑定目标。
        add_requirement(self.paths, "被验证的要求")
        self.clean = json.loads(self.paths.state.read_text(encoding="utf-8"))

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _corrupt(self, mutate) -> None:
        state = copy.deepcopy(self.clean)
        mutate(state)
        self.paths.state.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")

    def test_corrupted_states_are_rejected_by_load_state_and_gate(self) -> None:
        cases = {
            "contract_empty": lambda s: s.update(contract={}),
            "contract_enabled_missing": lambda s: s.update(contract={"created_at": "x", "updated_at": "x"}),
            "contract_enabled_string": lambda s: s.update(contract={"enabled": "true", "created_at": "x", "updated_at": "x"}),
            "contract_enabled_int": lambda s: s.update(contract={"enabled": 1, "created_at": "x", "updated_at": "x"}),
            "contract_enabled_none": lambda s: s.update(contract={"enabled": None, "created_at": "x", "updated_at": "x"}),
            "contract_missing_timestamp": lambda s: s.update(contract={"enabled": True, "created_at": "x"}),
            "duplicate_requirement_id_case_insensitive": lambda s: s["requirements"].append(
                _requirement("r001", text="大小写不同的重复 ID")
            ),
            "duplicate_evidence_id_case_insensitive": lambda s: s["evidence"].extend(
                [
                    {"id": "e001", "requirement_id": "R001", "requirement_revision": 1, "result": "success", "summary": "s", "created_at": "x"},
                    {"id": "E001", "requirement_id": "R001", "requirement_revision": 1, "result": "failed", "summary": "s", "created_at": "x"},
                ]
            ),
            "requirement_bad_status": lambda s: s["requirements"][0].update(status="finished"),
            "requirement_bad_kind": lambda s: s["requirements"][0].update(kind="wish"),
            "requirement_revision_bool": lambda s: s["requirements"][0].update(revision=True),
            "requirement_revision_zero": lambda s: s["requirements"][0].update(revision=0),
            "requirement_missing_history": lambda s: s["requirements"][0].pop("history"),
            "requirement_not_object": lambda s: s["requirements"].append("junk"),
            "evidence_missing_binding": lambda s: s["evidence"].append(
                {"id": "E001", "requirement_id": "R999", "requirement_revision": 1, "result": "success", "summary": "s", "created_at": "x"}
            ),
            "evidence_future_revision": lambda s: s["evidence"].append(
                {"id": "E001", "requirement_id": "R001", "requirement_revision": 9, "result": "success", "summary": "s", "created_at": "x"}
            ),
            "evidence_bad_result": lambda s: s["evidence"].append(
                {"id": "E001", "requirement_id": "R001", "requirement_revision": 1, "result": "pass", "summary": "s", "created_at": "x"}
            ),
            "evidence_missing_summary": lambda s: s["evidence"].append(
                {"id": "E001", "requirement_id": "R001", "requirement_revision": 1, "result": "success", "created_at": "x"}
            ),
            "evidence_revision_string": lambda s: s["evidence"].append(
                {"id": "E001", "requirement_id": "R001", "requirement_revision": "1", "result": "success", "summary": "s", "created_at": "x"}
            ),
            "note_not_object": lambda s: s["notes"].append("junk"),
            "note_missing_text": lambda s: s["notes"].append({"id": "N001", "kind": "note", "source": "manual", "created_at": "x"}),
            "project_missing_name": lambda s: s["project"].pop("name"),
            "top_level_missing_created_at": lambda s: s.pop("created_at"),
        }
        for label, mutate in cases.items():
            with self.subTest(case=label):
                self._corrupt(mutate)
                with self.assertRaises(GuardError, msg=f"损坏场景 {label} 必须被 load_state 拒绝"):
                    load_state(self.paths)
                # 门禁入口同样不能放行：check_gate 复用 load_state 的严格校验。
                with self.assertRaises(GuardError, msg=f"损坏场景 {label} 必须被 check_gate 拒绝"):
                    check_gate(self.paths)

    def test_corrupted_state_is_preserved_and_not_rebuilt_or_closed(self) -> None:
        # 损坏现场必须保留原文件：不能重建空账本，也不能悄悄改成 enabled=false。
        self._corrupt(lambda s: s.update(contract={}))
        corrupted_raw = self.paths.state.read_text(encoding="utf-8")
        with self.assertRaises(GuardError):
            check_gate(self.paths)
        self.assertEqual(self.paths.state.read_text(encoding="utf-8"), corrupted_raw, "失败的读取不得改写状态文件")
        state = json.loads(corrupted_raw)
        self.assertEqual(state["contract"], {}, "不得重建/改写 contract")

    def test_corrupted_state_fails_cli_gate_with_error_exit(self) -> None:
        from context_guard_lite.cli import main

        self._corrupt(lambda s: s.update(contract={}))
        self.assertEqual(main(["--root", self.root, "gate", "check"]), 2)

    def test_save_state_rejects_incomplete_state_before_write(self) -> None:
        # 写入侧防线：不完整状态不得落盘（程序 bug 的最后闸门）。
        state = copy.deepcopy(self.clean)
        state["contract"].pop("enabled")
        with self.assertRaises(GuardError):
            save_state(self.paths, state)
        on_disk = json.loads(self.paths.state.read_text(encoding="utf-8"))
        self.assertTrue(on_disk["contract"]["enabled"], "被拒绝的写入不得影响磁盘上的原状态")

    def test_explicit_disabled_and_empty_ledger_semantics_preserved(self) -> None:
        # 正例：用户明确 enabled=false 仍是 disabled 放行。
        self._corrupt(lambda s: s["contract"].update(enabled=False))
        gate = check_gate(self.paths)
        self.assertEqual((gate["ok"], gate["status"]), (True, "disabled"))
        # 正例：空账本仍是 idle 放行。
        import tempfile as _tempfile

        with _tempfile.TemporaryDirectory() as empty_root:
            init_project(empty_root, "empty")
            gate = check_gate(project_paths(empty_root))
            self.assertEqual((gate["ok"], gate["status"]), (True, "idle"))

    def test_valid_ledger_still_loads_and_gates(self) -> None:
        # 正例：合法账本（done + 当前版本 success evidence）照常通过。
        update_to_done = copy.deepcopy(self.clean)
        update_to_done["requirements"][0]["status"] = "done"
        update_to_done["evidence"].append(
            {"id": "E001", "requirement_id": "R001", "requirement_revision": 1, "result": "success", "summary": "已验证", "created_at": "2026-09-26T00:00:00Z"}
        )
        self.paths.state.write_text(json.dumps(update_to_done, ensure_ascii=False), encoding="utf-8")
        state = load_state(self.paths)
        self.assertEqual(state["requirements"][0]["id"], "R001")
        gate = check_gate(self.paths)
        self.assertTrue(gate["ok"])
        self.assertEqual(gate["status"], "pass")

    def test_add_evidence_still_works_end_to_end(self) -> None:
        # 正例：修复后正常 API 流程不受影响（防止校验误伤正常写入）。
        add_evidence(self.paths, "R001", "真实验证过", "success", target="src/main.py")
        state = load_state(self.paths)
        self.assertEqual(state["evidence"][0]["result"], "success")


if __name__ == "__main__":
    unittest.main()
