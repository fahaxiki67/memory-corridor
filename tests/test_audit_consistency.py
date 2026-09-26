"""MC-03 回归：状态提交与审计记录的一致性（故障注入）。

预审实证（2026-09-26）：命令先经 ``state_transaction`` 提交 state.json，之后
才独立追加 events/notebook；后续写入失败会造成「命令失败但状态已改变」，用户
无法区分两者，还会盲目重试导致重复记账。

本文件锁定修复后的语义：

- 真源：state.json（原子替换写 + 跨进程锁内读改写 = 提交点）；
- 事件：带唯一幂等 id；events.jsonl 写入失败暂存 events.pending.jsonl，
  下一条命令锁内按 id 去重补记——审计最终一致，不假装成功；
- 暂存也失败：抛 OSError，CLI 退出 2 并提示「状态可能已提交，请核对」；
- notebook：可重建的衍生记录，写入失败只警告不失败。

故障注入方式：把目标文件替换成同名目录（POSIX 与 Windows 上对目录
open("a") 都会失败），不依赖 monkeypatch，真实走文件系统错误路径。
"""

from __future__ import annotations

import io
import json
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path

from context_guard_lite.cli import main
from context_guard_lite.contract import (
    flush_pending_events,
    init_project,
    load_state,
    project_paths,
)
from context_guard_lite.evidence import add_evidence
from context_guard_lite.requirements import add_requirement

REPO_ROOT = Path(__file__).resolve().parents[1]


def _events_path(paths) -> Path:
    return paths.events


def _make_write_fail(path: Path) -> None:
    """把文件换成同名目录，使后续 open(..., 'a') 以 OSError 失败。"""
    if path.exists():
        path.unlink()
    path.mkdir()


class AuditConsistencyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        init_project(self.root, "audit")
        self.paths = project_paths(self.root)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _event_lines(self) -> list[dict]:
        return [json.loads(line) for line in self.paths.events.read_text(encoding="utf-8").splitlines() if line.strip()]

    def test_all_events_carry_unique_ids(self) -> None:
        requirement = add_requirement(self.paths, "带审计的要求")
        add_evidence(self.paths, requirement["id"], "验证", "success")
        events = self._event_lines()
        self.assertTrue(events)
        ids = [event.get("id") for event in events]
        self.assertTrue(all(ids), "每条事件都必须有幂等 id")
        self.assertEqual(len(ids), len(set(ids)), "事件 id 不得重复")

    def test_event_write_failure_defers_to_pending_and_replays(self) -> None:
        add_requirement(self.paths, "状态先提交")
        # 注入：events.jsonl 只读（保留历史内容，新追加以 PermissionError 失败）。
        self.paths.events.chmod(0o444)
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            add_requirement(self.paths, "审计失败也不丢")
        self.assertIn("审计失败也不丢", json.dumps(load_state(self.paths), ensure_ascii=False))
        pending_path = self.paths.data / "events.pending.jsonl"
        self.assertTrue(pending_path.is_file(), "失败事件必须暂存")
        pending_events = [json.loads(line) for line in pending_path.read_text(encoding="utf-8").splitlines() if line.strip()]
        self.assertEqual(len(pending_events), 1)
        self.assertEqual(pending_events[0]["type"], "requirement.add")
        # 恢复后任意一条命令都会幂等补记。
        self.paths.events.chmod(0o644)
        with redirect_stderr(stderr):
            self.assertEqual(main(["--root", str(self.root), "status"]), 0)
        events = self._event_lines()
        add_events = [event for event in events if event["type"] == "requirement.add"]
        self.assertEqual(len(add_events), 2, "应包含注入前的 1 条与补记的 1 条")
        self.assertEqual(len({event["id"] for event in events}), len(events), "补记不得产生重复 id")
        self.assertFalse(pending_path.exists(), "补记完成后暂存文件应清理")
        self.assertIn("补记", stderr.getvalue())

    def test_replay_dedups_when_crash_hits_between_write_and_cleanup(self) -> None:
        _make_write_fail(self.paths.events)
        add_requirement(self.paths, "可能崩在写入与清理之间")
        pending_path = self.paths.data / "events.pending.jsonl"
        pending_line = pending_path.read_text(encoding="utf-8").strip()
        self.paths.events.rmdir()
        # 模拟「事件已写入、pending 未清理」的崩溃现场：events.jsonl 已含该行。
        self.paths.events.write_text(pending_line + "\n", encoding="utf-8")
        with redirect_stderr(io.StringIO()):
            replayed, _ = flush_pending_events(self.paths)
        self.assertEqual(replayed, 0, "事件已在 events.jsonl 中时重放必须跳过")
        events = self._event_lines()
        self.assertEqual(len(events), 1, "注入重建的日志只含该事件，重放不得追加重复行")
        self.assertEqual(json.loads(pending_line)["id"], events[0]["id"])

    def test_pending_also_failing_raises_but_state_stays_committed(self) -> None:
        _make_write_fail(self.paths.events)
        _make_write_fail(self.paths.data / "events.pending.jsonl")
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            exit_code = main(["--root", str(self.root), "requirements", "add", "磁盘全坏"])
        self.assertEqual(exit_code, 2, "审计通道彻底不可写时必须如实报错")
        state = load_state(self.paths)
        texts = [item["text"] for item in state["requirements"]]
        self.assertIn("磁盘全坏", texts, "状态在报错前已经提交，必须如实保留")
        self.assertIn("核对", stderr.getvalue(), "错误输出必须提示状态可能已变化")

    def test_notebook_failure_warns_without_failing_command(self) -> None:
        _make_write_fail(self.paths.notebook)
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            exit_code = main(["--root", str(self.root), "note", "add", "notebook 坏了也要记账"])
        self.assertEqual(exit_code, 0, "notebook 是可重建的衍生文件，失败不应让命令报错")
        state = load_state(self.paths)
        self.assertEqual(state["notes"][-1]["text"], "notebook 坏了也要记账")
        self.assertIn("警告", stderr.getvalue())
        self.assertIn("旁记事本", stderr.getvalue())

    def test_malformed_pending_lines_are_dropped_with_warning(self) -> None:
        pending_path = self.paths.data / "events.pending.jsonl"
        good = json.dumps({"at": "2026-09-26T00:00:00Z", "type": "note.add", "id": "abc123"}, ensure_ascii=False)
        pending_path.write_text(f"{good}\n{{截断的半行\n", encoding="utf-8")
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            replayed, leftover = flush_pending_events(self.paths)
        self.assertEqual(replayed, 1)
        self.assertEqual(leftover, 0)
        self.assertIn("无法解析", stderr.getvalue())
        self.assertFalse(pending_path.exists())
        events = self._event_lines()
        self.assertEqual(sum(1 for event in events if event.get("id") == "abc123"), 1)

    def test_concurrent_replay_keeps_each_event_once(self) -> None:
        """多进程同时补记：锁 + 幂等 id 必须保证恰好一条（覆盖中断后重试场景）。"""
        pending_path = self.paths.data / "events.pending.jsonl"
        lines = [
            json.dumps({"at": "2026-09-26T00:00:00Z", "type": "requirement.add", "id": f"evt{i:02d}"}, ensure_ascii=False)
            for i in range(5)
        ]
        pending_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        workers = 4
        processes = [
            subprocess.Popen(
                [sys.executable, "-m", "context_guard_lite", "--root", str(self.root), "status"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                cwd=REPO_ROOT,
            )
            for _ in range(workers)
        ]
        for process in processes:
            self.assertEqual(process.wait(timeout=60), 0)
        events = self._event_lines()
        replayed_ids = [event["id"] for event in events if str(event.get("id", "")).startswith("evt")]
        self.assertEqual(sorted(replayed_ids), [f"evt{i:02d}" for i in range(5)], "并发补记必须恰好一次、不重不丢")
        self.assertFalse(pending_path.exists())


if __name__ == "__main__":
    unittest.main()
