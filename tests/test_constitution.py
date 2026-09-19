"""项目章程（constitution）测试。

覆盖四组契约：
1. set/show/clear 子命令：幂等、原子写（无临时文件残留）、UTF-8、人可直接编辑；
2. 「章程约束」区块在恢复包 / status / Stop hook 阻塞清单三处同源一致；
3. 未设置章程时各输出不出现区块，status --json 为 null（缺失≠0）；
4. 章程只展示不判定：不改变门禁判定，读取失败显式占位且不破坏门禁链路。
"""

from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from context_guard_lite.cli import main
from context_guard_lite.constitution import (
    add_constraint,
    clear_constitution,
    constitution_summary,
    load_constraints,
)
from context_guard_lite.contract import (
    GuardError,
    init_project,
    load_state,
    project_paths,
    read_events,
)
from context_guard_lite.evidence import add_evidence
from context_guard_lite.gate import check_gate
from context_guard_lite.integrations.codex import handle_hook_event
from context_guard_lite.integrations.zcode import handle_zcode_hook_event
from context_guard_lite.recovery import build_packet
from context_guard_lite.requirements import add_requirement, update_requirement

SECTION_HEADER_MARK = "## 章程约束"
CONSTRAINT_A = "不得修改数据库结构"
CONSTRAINT_B = "每次交付前全量测试必须通过"


def _codex_stop_payload(cwd: Path) -> dict:
    return {
        "hook_event_name": "Stop",
        "cwd": str(cwd),
        "session_id": "s-1",
        "transcript_path": None,
        "stop_hook_active": False,
        "turn_id": "t-1",
        "last_assistant_message": None,
        "model": "gpt-5",
        "permission_mode": "default",
    }


def _zcode_stop_payload(cwd: Path) -> dict:
    return {
        "hook_event_name": "Stop",
        "cwd": str(cwd),
        "session_id": "s-1",
        "stop_hook_active": False,
        "permission_mode": "default",
    }


def _constraint_rows(text: str) -> list[str]:
    """从任意输出中提取「章程约束」区块的约束行（标题后连续的 "- " 行）。"""
    lines = text.splitlines()
    start = next(index for index, line in enumerate(lines) if line.startswith(SECTION_HEADER_MARK))
    rows: list[str] = []
    for line in lines[start + 1 :]:
        if line.startswith("- "):
            rows.append(line)
        elif rows:
            break
    return rows


class ConstitutionCommandTests(unittest.TestCase):
    """set/show/clear：幂等、原子写、UTF-8、人可直接编辑。"""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        init_project(self.root, "constitution-project")
        self.paths = project_paths(self.root)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _run(self, argv: list[str]) -> tuple[int, str]:
        with contextlib.redirect_stdout(io.StringIO()) as fake:
            code = main(["--root", str(self.root), *argv])
        return code, fake.getvalue()

    def test_set_creates_utf8_file_and_show_lists_constraints(self) -> None:
        code, out = self._run(["constitution", "set", CONSTRAINT_A])
        self.assertEqual(code, 0)
        self.assertIn("已写入章程（当前 1 条）", out)

        code, out = self._run(["constitution", "set", "每次交付前全量测试必须通过\n第二行会被折叠"])
        self.assertEqual(code, 0)
        self.assertIn("已写入章程（当前 2 条）", out)

        # UTF-8 落盘、一行一条、换行折叠为单行
        content = self.paths.constitution.read_text(encoding="utf-8")
        self.assertIn("# 项目章程", content)
        self.assertIn(CONSTRAINT_A, content)
        self.assertIn("每次交付前全量测试必须通过 第二行会被折叠", content)

        code, out = self._run(["constitution", "show"])
        self.assertEqual(code, 0)
        self.assertIn("章程约束（2 条", out)
        self.assertIn(f"1. {CONSTRAINT_A}", out)
        self.assertIn("2. 每次交付前全量测试必须通过 第二行会被折叠", out)
        self.assertIn("只展示不判定", out)

    def test_set_same_text_is_idempotent(self) -> None:
        self._run(["constitution", "set", CONSTRAINT_A])
        before = self.paths.constitution.read_bytes()
        code, out = self._run(["constitution", "set", f"  {CONSTRAINT_A}  "])
        self.assertEqual(code, 0)
        self.assertIn("已存在", out)
        self.assertNotIn("已写入章程", out)
        # 幂等：文件字节不变，事件日志也不重复记录
        self.assertEqual(self.paths.constitution.read_bytes(), before)
        self.assertEqual(len(read_events(self.paths, event_type="constitution.set")), 1)

    def test_set_appends_without_destroying_manual_edits(self) -> None:
        self.paths.constitution.write_text(
            "# 项目章程（Constitution）\n<!-- 我的自定义说明 -->\n\n手工写入的约束\n# 这行是注释\n",
            encoding="utf-8",
        )
        result = add_constraint(self.paths, "程序追加的约束")
        self.assertEqual(result["status"], "added")
        self.assertEqual(result["count"], 2)
        content = self.paths.constitution.read_text(encoding="utf-8")
        self.assertIn("<!-- 我的自定义说明 -->", content)
        self.assertIn("手工写入的约束", content)
        self.assertIn("程序追加的约束", content)
        self.assertEqual(load_constraints(self.paths), ["手工写入的约束", "程序追加的约束"])

    def test_set_rejects_empty_text(self) -> None:
        with self.assertRaises(GuardError):
            add_constraint(self.paths, "   ")
        self.assertFalse(self.paths.constitution.exists())

    def test_show_without_constitution_says_not_set(self) -> None:
        code, out = self._run(["constitution", "show"])
        self.assertEqual(code, 0)
        self.assertIn("未设置章程", out)

    def test_show_with_zero_valid_lines_is_explicit(self) -> None:
        self.paths.constitution.write_text("# 项目章程\n\n# 只有注释\n", encoding="utf-8")
        code, out = self._run(["constitution", "show"])
        self.assertEqual(code, 0)
        self.assertIn("没有有效约束行", out)

    def test_clear_is_idempotent_and_allows_reset(self) -> None:
        self._run(["constitution", "set", CONSTRAINT_A])
        code, out = self._run(["constitution", "clear"])
        self.assertEqual(code, 0)
        self.assertIn("已清除章程", out)
        self.assertFalse(self.paths.constitution.exists())
        self.assertEqual(len(read_events(self.paths, event_type="constitution.clear")), 1)

        code, out = self._run(["constitution", "clear"])
        self.assertEqual(code, 0)
        self.assertIn("未设置章程，无需清除", out)
        self.assertEqual(len(read_events(self.paths, event_type="constitution.clear")), 1)

        code, out = self._run(["constitution", "set", "重置后的约束"])
        self.assertEqual(code, 0)
        self.assertIn("已写入章程（当前 1 条）", out)

    def test_atomic_write_leaves_no_temp_files(self) -> None:
        for text in ("约束一", "约束二", "约束三"):
            self._run(["constitution", "set", text])
        leftovers = [item.name for item in self.paths.data.iterdir() if item.name.startswith(".constitution.md.")]
        self.assertEqual(leftovers, [])
        self.assertEqual(len(load_constraints(self.paths)), 3)

    def test_state_json_gains_no_new_fields(self) -> None:
        keys_before = set(load_state(self.paths))
        self._run(["constitution", "set", CONSTRAINT_A])
        self._run(["constitution", "clear"])
        self.assertEqual(set(load_state(self.paths)), keys_before)
        self.assertNotIn("constitution", load_state(self.paths))


class ConstitutionSectionTests(unittest.TestCase):
    """「章程约束」区块：恢复包 / status / Stop hook 阻塞清单三处同源一致。"""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        init_project(self.root, "section-project")
        self.paths = project_paths(self.root)
        add_constraint(self.paths, CONSTRAINT_A)
        add_constraint(self.paths, CONSTRAINT_B)
        add_requirement(self.paths, "未完成的验收项")  # 让门禁处于 blocked

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _status_text(self) -> str:
        with contextlib.redirect_stdout(io.StringIO()) as fake:
            self.assertEqual(main(["--root", str(self.root), "status"]), 0)
        return fake.getvalue()

    def _status_json(self) -> dict:
        with contextlib.redirect_stdout(io.StringIO()) as fake:
            self.assertEqual(main(["--root", str(self.root), "status", "--json"]), 0)
        return json.loads(fake.getvalue())

    def test_section_consistent_across_recovery_status_and_stop_hooks(self) -> None:
        packet = build_packet(self.paths)
        status_text = self._status_text()
        codex_reason = handle_hook_event(_codex_stop_payload(self.root)).output["reason"]
        zcode_reason = handle_zcode_hook_event(_zcode_stop_payload(self.root)).output["reason"]

        for text in (packet, status_text, codex_reason, zcode_reason):
            self.assertIn(f"{SECTION_HEADER_MARK}（2 条）", text)
            self.assertIn(CONSTRAINT_A, text)
            self.assertIn(CONSTRAINT_B, text)
            self.assertIn("只展示不判定", text)

        rows = _constraint_rows(packet)
        self.assertEqual(rows, [f"- {CONSTRAINT_A}", f"- {CONSTRAINT_B}"])
        self.assertEqual(_constraint_rows(status_text), rows)
        self.assertEqual(_constraint_rows(codex_reason), rows)
        self.assertEqual(_constraint_rows(zcode_reason), rows)

        parsed = self._status_json()
        self.assertEqual(parsed["constitution"]["count"], 2)
        self.assertEqual(parsed["constitution"]["constraints"], [CONSTRAINT_A, CONSTRAINT_B])

    def test_display_only_gate_judgement_unchanged(self) -> None:
        # 章程是纯展示：门禁结果与状态里都没有它的位置，设置与否不改变判定。
        gate_with_constitution = check_gate(self.paths)
        self.assertFalse(gate_with_constitution["ok"])  # 只因 requirement 未完成而阻塞
        reasons = [reason for item in gate_with_constitution["blocking"] for reason in item["reasons"]]
        self.assertTrue(all("章程" not in reason for reason in reasons))

        clear_constitution(self.paths)
        self.assertEqual(check_gate(self.paths), gate_with_constitution)

        # requirement 正常补证后门禁照常放行，与章程存在与否无关
        add_evidence(self.paths, "R001", "真实验证通过", "success")
        update_requirement(self.paths, "R001", status="done")
        self.assertTrue(check_gate(self.paths)["ok"])
        # 门禁 pass 后恢复包仍展示章程区块
        add_constraint(self.paths, CONSTRAINT_A)
        self.assertIn(f"{SECTION_HEADER_MARK}（1 条）", build_packet(self.paths))


class ConstitutionAbsentTests(unittest.TestCase):
    """未设置章程：各输出不出现区块；缺失≠0（status --json 为 null）。"""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        init_project(self.root, "absent-project")
        self.paths = project_paths(self.root)
        add_requirement(self.paths, "未完成的验收项")

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_no_constitution_no_section_anywhere(self) -> None:
        self.assertNotIn("章程约束", build_packet(self.paths))
        self.assertIsNone(constitution_summary(self.paths))

        with contextlib.redirect_stdout(io.StringIO()) as fake:
            self.assertEqual(main(["--root", str(self.root), "status"]), 0)
        self.assertNotIn("章程约束", fake.getvalue())

        with contextlib.redirect_stdout(io.StringIO()) as fake:
            self.assertEqual(main(["--root", str(self.root), "status", "--json"]), 0)
        self.assertIsNone(json.loads(fake.getvalue())["constitution"])

        codex_reason = handle_hook_event(_codex_stop_payload(self.root)).output["reason"]
        zcode_reason = handle_zcode_hook_event(_zcode_stop_payload(self.root)).output["reason"]
        self.assertNotIn("章程约束", codex_reason)
        self.assertNotIn("章程约束", zcode_reason)


class ConstitutionFailureTests(unittest.TestCase):
    """章程文件不可读：渲染显式占位而非静默，且不破坏门禁链路。"""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        init_project(self.root, "failure-project")
        self.paths = project_paths(self.root)
        add_requirement(self.paths, "未完成的验收项")
        # 文件位置被目录占位 → 读取必失败（跨平台可复现的 OSError）
        self.paths.constitution.mkdir()

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_unreadable_constitution_shows_placeholder_and_gate_unaffected(self) -> None:
        packet = build_packet(self.paths)
        self.assertIn(f"{SECTION_HEADER_MARK}（读取失败）", packet)
        self.assertIn("无法读取章程文件", packet)

        with contextlib.redirect_stdout(io.StringIO()) as fake:
            self.assertEqual(main(["--root", str(self.root), "status"]), 0)
        self.assertIn("读取失败", fake.getvalue())

        with contextlib.redirect_stdout(io.StringIO()) as fake:
            self.assertEqual(main(["--root", str(self.root), "status", "--json"]), 0)
        self.assertIn("error", json.loads(fake.getvalue())["constitution"])

        # 门禁照常按账本判定；Stop hook 不因章程读取失败而崩溃
        self.assertFalse(check_gate(self.paths)["ok"])
        codex_reason = handle_hook_event(_codex_stop_payload(self.root)).output["reason"]
        self.assertIn("读取失败", codex_reason)
        zcode_reason = handle_zcode_hook_event(_zcode_stop_payload(self.root)).output["reason"]
        self.assertIn("读取失败", zcode_reason)


if __name__ == "__main__":
    unittest.main()
