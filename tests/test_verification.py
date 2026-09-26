"""MC-02 回归：区分手工声明（manual）与执行验证（execution/strict 验收）。

预审实证（2026-09-26）：``evidence add`` 的 result/command/summary 全部可手填，
门禁只看 requirement revision + result==success——「我声明我跑过」与「我真的
跑过」不可区分。本文件锁定修复后的行为：

- ``evidence verify`` 真实执行命令（argv + 必设超时，不经 shell），退出码推导
  result，记录 argv/cwd/起止时间/退出码/超时/日志与日志 hash/绑定 revision/
  执行时工作树指纹；
- strict 开启时：手工 success 不满足门禁；执行过的 success 还要求退出码为零、
  日志在且未被改、工作树指纹未变（覆盖未提交修改，不是只绑 HEAD）；
- strict 关闭时既有行为完全不变（向后兼容正例）。

边界：防误报不防篡改——同权限用户仍可改文件与账本；指纹刻意排除
.context-guard/（日志/账本自身不得进入指纹，否则记录证据即自失效）。
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from context_guard_lite.cli import main
from context_guard_lite.contract import (
    GuardError,
    init_project,
    load_state,
    project_paths,
    set_strict,
)
from context_guard_lite.evidence import add_evidence
from context_guard_lite.gate import check_gate
from context_guard_lite.requirements import add_requirement, update_requirement
from context_guard_lite.verification import worktree_fingerprint

GIT_AVAILABLE = shutil.which("git") is not None


def _git(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(root), *args],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )


class VerificationWorkspace(unittest.TestCase):
    """带本地 git 仓库的初始化项目；无 git 环境时跳过 git 相关断言。"""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.paths = project_paths(self.root)
        if GIT_AVAILABLE:
            self.assertTrue(_git(self.root, "init").returncode == 0)
            _git(self.root, "config", "user.email", "test@example.com")
            _git(self.root, "config", "user.name", "test")
            (self.root / "app.py").write_text("print('v1')\n", encoding="utf-8")
            _git(self.root, "add", "-A")
            self.assertEqual(_git(self.root, "commit", "-m", "init").returncode, 0)
        init_project(self.root, "verify")
        self.python = sys.executable

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def verify_ok(self, requirement_id: str = "R001", summary: str = "真实验证通过", timeout: float = 60.0) -> dict:
        return main(
            [
                "--root",
                str(self.root),
                "evidence",
                "verify",
                "--for",
                requirement_id,
                "--summary",
                summary,
                "--timeout",
                str(timeout),
                "--",
                self.python,
                "-c",
                "print('ok')",
            ]
        )


class ExecutionEvidenceTests(VerificationWorkspace):
    def test_verify_records_real_execution_details(self) -> None:
        requirement = add_requirement(self.paths, "跑通冒烟")
        self.assertEqual(self.verify_ok(), 0)
        state = load_state(self.paths)
        evidence = state["evidence"][-1]
        verification = evidence["verification"]
        self.assertEqual(evidence["result"], "success")
        self.assertEqual(verification["mode"], "execution")
        self.assertEqual(verification["exit_code"], 0)
        self.assertFalse(verification["timed_out"])
        self.assertEqual(verification["argv"], [self.python, "-c", "print('ok')"])
        # project_paths 会 resolve 根目录（macOS 上 /var 是 /private/var 的符号链接）。
        self.assertEqual(verification["cwd"], str(self.root.resolve()))
        self.assertEqual(verification["requirement_revision"], requirement["revision"])
        log_path = self.paths.data / verification["log_file"]
        self.assertTrue(log_path.is_file(), "执行日志必须落盘")
        import hashlib

        self.assertEqual(
            hashlib.sha256(log_path.read_bytes()).hexdigest(), verification["log_sha256"], "日志 hash 必须与文件一致"
        )
        self.assertIn("ok", log_path.read_text(encoding="utf-8"))

    def test_verify_derives_failed_from_nonzero_exit(self) -> None:
        add_requirement(self.paths, "失败也要如实记录")
        result = main(
            [
                "--root",
                str(self.root),
                "evidence",
                "verify",
                "--for",
                "R001",
                "--summary",
                "注定失败",
                "--timeout",
                "30",
                "--",
                self.python,
                "-c",
                "raise SystemExit(3)",
            ]
        )
        self.assertEqual(result, 0, "记录 failed evidence 本身是成功操作")
        evidence = load_state(self.paths)["evidence"][-1]
        self.assertEqual(evidence["result"], "failed")
        self.assertEqual(evidence["verification"]["exit_code"], 3)

    def test_verify_timeout_records_failed_with_marker(self) -> None:
        add_requirement(self.paths, "超时不算通过")
        result = main(
            [
                "--root",
                str(self.root),
                "evidence",
                "verify",
                "--for",
                "R001",
                "--summary",
                "挂死的命令",
                "--timeout",
                "1",
                "--",
                self.python,
                "-c",
                "import time; time.sleep(10)",
            ]
        )
        self.assertEqual(result, 0)
        evidence = load_state(self.paths)["evidence"][-1]
        self.assertEqual(evidence["result"], "failed")
        self.assertTrue(evidence["verification"]["timed_out"])

    def test_verify_launch_failure_records_failed(self) -> None:
        add_requirement(self.paths, "命令不存在不能冒充执行")
        result = main(
            [
                "--root",
                str(self.root),
                "evidence",
                "verify",
                "--for",
                "R001",
                "--summary",
                "不存在的命令",
                "--timeout",
                "10",
                "--",
                "/definitely/not/a/real/binary",
            ]
        )
        self.assertEqual(result, 0)
        evidence = load_state(self.paths)["evidence"][-1]
        self.assertEqual(evidence["result"], "failed")
        self.assertIsNotNone(evidence["verification"]["launch_error"])
        self.assertIsNone(evidence["verification"]["exit_code"])

    def test_commands_run_as_argv_without_shell(self) -> None:
        # shell 元字符必须作为普通参数字面传递，不做管道/分号展开。
        add_requirement(self.paths, "argv 直传")
        main(
            [
                "--root",
                str(self.root),
                "evidence",
                "verify",
                "--for",
                "R001",
                "--summary",
                "元字符字面传递",
                "--timeout",
                "30",
                "--",
                self.python,
                "-c",
                "import sys; print(sys.argv[1])",
                "a;b|c&&d",
            ]
        )
        verification = load_state(self.paths)["evidence"][-1]["verification"]
        log_text = (self.paths.data / verification["log_file"]).read_text(encoding="utf-8")
        self.assertIn("a;b|c&&d", log_text)
        self.assertEqual(verification["exit_code"], 0)

    def test_verify_requires_command_and_positive_timeout(self) -> None:
        add_requirement(self.paths, "参数防线")
        # CLI 入口把 GuardError 转成退出码 2（与仓库既有约定一致）。
        self.assertEqual(
            main(["--root", str(self.root), "evidence", "verify", "--for", "R001", "--summary", "空命令"]), 2
        )
        self.assertEqual(
            main(
                [
                    "--root",
                    str(self.root),
                    "evidence",
                    "verify",
                    "--for",
                    "R001",
                    "--summary",
                    "零超时",
                    "--timeout",
                    "0",
                    "--",
                    self.python,
                    "-c",
                    "print('x')",
                ]
            ),
            2,
        )


class StrictGateTests(VerificationWorkspace):
    def _done_with_manual_success(self) -> None:
        add_requirement(self.paths, "被验证的要求")
        add_evidence(self.paths, "R001", "我声明我跑过", "success")
        update_requirement(self.paths, "R001", status="done")

    def test_strict_off_keeps_legacy_behavior(self) -> None:
        # 向后兼容正例：strict 关闭时手工 success 照旧满足门禁。
        self._done_with_manual_success()
        gate = check_gate(self.paths)
        self.assertTrue(gate["ok"])
        set_strict(self.paths, False)
        self.assertTrue(check_gate(self.paths)["ok"])

    def test_strict_blocks_manual_success(self) -> None:
        self._done_with_manual_success()
        set_strict(self.paths, True)
        gate = check_gate(self.paths)
        self.assertFalse(gate["ok"])
        reasons = gate["blocking"][0]["reasons"]
        self.assertTrue(any("manual" in reason for reason in reasons), reasons)
        # 补一条真实执行的 success 后 strict 门禁通过。
        self.assertEqual(self.verify_ok(), 0)
        self.assertTrue(check_gate(self.paths)["ok"])

    def test_strict_gate_stale_after_uncommitted_code_change(self) -> None:
        if not GIT_AVAILABLE:
            self.skipTest("git 不可用")
        add_requirement(self.paths, "代码变化必须重新验证")
        self.assertEqual(self.verify_ok(), 0)
        update_requirement(self.paths, "R001", status="done")
        set_strict(self.paths, True)
        self.assertTrue(check_gate(self.paths)["ok"])
        # 未提交的一行修改必须让证据过期（不能只绑 HEAD）。
        (self.root / "app.py").write_text("print('v2')\n", encoding="utf-8")
        gate = check_gate(self.paths)
        self.assertFalse(gate["ok"], "未提交代码变化后 strict 门禁必须阻塞")
        self.assertTrue(any("指纹" in reason for reason in gate["blocking"][0]["reasons"]))
        # 还原后恢复 PASS。
        _git(self.root, "checkout", "--", "app.py")
        self.assertTrue(check_gate(self.paths)["ok"])

    def test_strict_gate_blocks_on_log_missing_and_tamper(self) -> None:
        add_requirement(self.paths, "日志是执行证据的一部分")
        self.assertEqual(self.verify_ok(), 0)
        update_requirement(self.paths, "R001", status="done")
        set_strict(self.paths, True)
        verification = load_state(self.paths)["evidence"][-1]["verification"]
        log_path = self.paths.data / verification["log_file"]
        log_path.unlink()
        gate = check_gate(self.paths)
        self.assertFalse(gate["ok"])
        self.assertTrue(any("日志缺失" in reason for reason in gate["blocking"][0]["reasons"]))
        # 日志被改动（hash 不一致）同样阻塞。
        log_path.write_text("hash: 被改过的日志\n", encoding="utf-8")
        gate = check_gate(self.paths)
        self.assertFalse(gate["ok"])
        self.assertTrue(any("hash 不一致" in reason for reason in gate["blocking"][0]["reasons"]))

    def test_strict_flag_persisted_and_validated(self) -> None:
        set_strict(self.paths, True)
        self.assertTrue(load_state(self.paths)["contract"]["strict"])
        # 损坏的 strict 值不能被宽容读取（与 MC-01 同一纪律）。
        state_path = self.paths.state
        state = json.loads(state_path.read_text(encoding="utf-8"))
        state["contract"]["strict"] = "on"
        state_path.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
        with self.assertRaises(GuardError):
            load_state(self.paths)

    def test_mark_done_hint_respects_strict(self) -> None:
        from context_guard_lite.requirements import mark_done

        add_requirement(self.paths, "done 提示与门禁一致")
        add_evidence(self.paths, "R001", "手工声明", "success")
        set_strict(self.paths, True)
        _, satisfied = mark_done(self.paths, "R001")
        self.assertFalse(satisfied, "strict 下手工 success 不能提示「已满足」")
        self.assertFalse(check_gate(self.paths)["ok"])
        self.assertEqual(self.verify_ok(), 0)
        _, satisfied_after = mark_done(self.paths, "R001")
        # mark_done 幂等重跑：执行证据就位后提示转正，且与门禁口径一致。
        self.assertTrue(satisfied_after)
        self.assertTrue(check_gate(self.paths)["ok"])

    def test_recovery_and_list_show_execution_mode(self) -> None:
        add_requirement(self.paths, "可见性")
        self.assertEqual(self.verify_ok(), 0)
        listing = main(["--root", str(self.root), "evidence", "list"])
        self.assertEqual(listing, 0)
        from context_guard_lite.recovery import build_packet

        packet = build_packet(self.paths)
        self.assertIn("execution", packet)


class FingerprintTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_non_git_directory_returns_none(self) -> None:
        self.assertIsNone(worktree_fingerprint(self.root))

    def test_unborn_repo_fingerprint_is_stable(self) -> None:
        if not GIT_AVAILABLE:
            self.skipTest("git 不可用")
        _git(self.root, "init")
        (self.root / "a.txt").write_text("hello\n", encoding="utf-8")
        first = worktree_fingerprint(self.root)
        self.assertIsNotNone(first)
        self.assertEqual(first, worktree_fingerprint(self.root))
        (self.root / "a.txt").write_text("changed\n", encoding="utf-8")
        self.assertNotEqual(first, worktree_fingerprint(self.root))

    def test_fingerprint_excludes_ledger_and_logs(self) -> None:
        if not GIT_AVAILABLE:
            self.skipTest("git 不可用")
        _git(self.root, "init")
        (self.root / "a.txt").write_text("hello\n", encoding="utf-8")
        _git(self.root, "add", "-A")
        _git(self.root, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-m", "init")
        # init_project 与真实使用一致：建立忽略 .context-guard/ 的 .gitignore。
        init_project(self.root, "fp")
        before = worktree_fingerprint(self.root)
        ledger = self.root / ".context-guard"
        (ledger / "logs").mkdir(parents=True, exist_ok=True)
        (ledger / "logs" / "E001.log").write_text("执行日志\n", encoding="utf-8")
        (ledger / "state.json").write_text("{}\n", encoding="utf-8")
        self.assertEqual(
            before,
            worktree_fingerprint(self.root),
            "账本/日志自身变化不得改变指纹（否则记录证据即自失效）",
        )


if __name__ == "__main__":
    unittest.main()
