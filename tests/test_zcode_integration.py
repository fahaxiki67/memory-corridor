"""ZCode 原生集成测试：Plugin 分发文件 + SessionStart/Stop 协议契约。

协议要点（见 integrations/zcode.py 模块文档）：
- ZCode 没有 PreCompact 事件；
- Stop 放行时必须输出空（``continue: true`` 在 ZCode Stop 上会触发续命）；
- 阻塞用 ``{"decision": "block", "reason": ...}``，且只在 stop_hook_active
  为假时阻塞一次（平台上限 3 次续命，本适配每回合最多用 1 次）。
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from context_guard_lite.cli import main
from context_guard_lite.contract import init_project, project_paths
from context_guard_lite.evidence import add_evidence
from context_guard_lite.integrations.zcode import (
    SESSION_START_MATCHER,
    handle_zcode_hook_event,
    installed_plugin_status,
    plugin_files_status,
    run_zcode_hook_command,
    zcode_hook_status,
)
from context_guard_lite.requirements import add_requirement, update_requirement

REPO_ROOT = Path(__file__).resolve().parent.parent
WRAPPER = REPO_ROOT / "hooks" / "zcode_hook.py"


def _stop_payload(cwd: Path, stop_hook_active: bool = False, snake: bool = True) -> dict:
    payload: dict = {
        "cwd": str(cwd),
        "session_id": "s-1",
        "sessionId": "s-1",
        "permission_mode": "default",
        "last_assistant_message": "我认为任务已完成。",
    }
    if snake:
        payload["hook_event_name"] = "Stop"
        payload["stop_hook_active"] = stop_hook_active
    else:
        payload["hookEventName"] = "Stop"
        payload["stopHookActive"] = stop_hook_active
    return payload


def _session_start_payload(cwd: Path, source: str) -> dict:
    return {
        "hook_event_name": "SessionStart",
        "hookEventName": "SessionStart",
        "cwd": str(cwd),
        "session_id": "s-1",
        "source": source,
        "permission_mode": "default",
    }


class ZCodeStopGateTests(unittest.TestCase):
    """Stop 门禁：放行输出必须为空；阻塞只发生一次。"""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.paths = project_paths(self.root)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_pass_allows_with_empty_output(self) -> None:
        init_project(self.root, "zcode-pass")
        add_requirement(self.paths, "必须有测试")
        add_evidence(self.paths, "R001", "测试全部通过", "success", command="python -m unittest")
        update_requirement(self.paths, "R001", status="done")
        outcome = handle_zcode_hook_event(_stop_payload(self.root))
        # 关键契约：ZCode 放行 = 空输出。绝不能是 {"continue": true}。
        self.assertIsNone(outcome.output)

    def test_open_requirement_blocks_once(self) -> None:
        init_project(self.root, "zcode-open")
        add_requirement(self.paths, "ZCode 侧未完成项")
        blocked = handle_zcode_hook_event(_stop_payload(self.root))
        self.assertEqual(blocked.output["decision"], "block")
        self.assertIn("R001", blocked.output["reason"])
        self.assertIn("不能结束", blocked.output["reason"])

    def test_camel_case_payload_is_accepted(self) -> None:
        init_project(self.root, "zcode-camel")
        add_requirement(self.paths, "camelCase 输入")
        blocked = handle_zcode_hook_event(_stop_payload(self.root, snake=False))
        self.assertEqual(blocked.output["decision"], "block")
        looping = handle_zcode_hook_event(_stop_payload(self.root, stop_hook_active=True, snake=False))
        self.assertIsNone(looping.output)

    def test_done_without_current_revision_success_evidence_blocks(self) -> None:
        init_project(self.root, "zcode-done-no-evidence")
        add_requirement(self.paths, "声称完成但没有证据")
        update_requirement(self.paths, "R001", status="done", reason="用户要求")
        blocked = handle_zcode_hook_event(_stop_payload(self.root))
        self.assertEqual(blocked.output["decision"], "block")
        self.assertIn("R001", blocked.output["reason"])
        self.assertIn("没有匹配当前版本的 evidence", blocked.output["reason"])

    def test_latest_failed_evidence_blocks(self) -> None:
        init_project(self.root, "zcode-failed")
        add_requirement(self.paths, "最新验证失败")
        add_evidence(self.paths, "R001", "早期通过", "success")
        add_evidence(self.paths, "R001", "回归失败", "failed")
        blocked = handle_zcode_hook_event(_stop_payload(self.root))
        self.assertEqual(blocked.output["decision"], "block")
        self.assertIn("failed", blocked.output["reason"])

    def test_latest_unknown_evidence_blocks(self) -> None:
        init_project(self.root, "zcode-unknown")
        add_requirement(self.paths, "验证结果未知")
        add_evidence(self.paths, "R001", "跑了但没看懂", "unknown")
        blocked = handle_zcode_hook_event(_stop_payload(self.root))
        self.assertEqual(blocked.output["decision"], "block")
        self.assertIn("unknown", blocked.output["reason"])

    def test_loop_guard_blocks_only_once(self) -> None:
        init_project(self.root, "zcode-loop")
        add_requirement(self.paths, "防续命循环")
        blocked = handle_zcode_hook_event(_stop_payload(self.root))
        self.assertEqual(blocked.output["decision"], "block")
        looping = handle_zcode_hook_event(_stop_payload(self.root, stop_hook_active=True))
        # stop_hook_active=true：不再阻塞（平台上限 3 次续命，本适配只用 1 次）。
        self.assertIsNone(looping.output)
        # 回归：diagnostics 必须是元组；少写尾逗号会退化成字符串被逐字符打印。
        self.assertEqual(len(looping.diagnostics), 1)
        self.assertIn("R001", looping.diagnostics[0])

    def test_disabled_protection_allows(self) -> None:
        init_project(self.root, "zcode-disabled")
        add_requirement(self.paths, "保护关闭后不阻塞")
        self.assertEqual(main(["--root", str(self.root), "off"]), 0)
        outcome = handle_zcode_hook_event(_stop_payload(self.root))
        self.assertIsNone(outcome.output)

    def test_idle_ledger_allows(self) -> None:
        init_project(self.root, "zcode-idle")
        outcome = handle_zcode_hook_event(_stop_payload(self.root))
        self.assertIsNone(outcome.output)

    def test_uninitialized_stop_is_noop(self) -> None:
        outcome = handle_zcode_hook_event(_stop_payload(self.root))
        self.assertIsNone(outcome.output)
        self.assertFalse((self.root / ".context-guard").exists())

    def test_corrupted_state_blocks_once(self) -> None:
        init_project(self.root, "zcode-corrupt")
        (self.paths.state).write_text("{broken", encoding="utf-8")
        blocked = handle_zcode_hook_event(_stop_payload(self.root))
        self.assertEqual(blocked.output["decision"], "block")
        self.assertIn("state.json", blocked.output["reason"])
        looping = handle_zcode_hook_event(_stop_payload(self.root, stop_hook_active=True))
        self.assertIsNone(looping.output)


class ZCodeSessionStartTests(unittest.TestCase):
    """SessionStart：startup/resume/compact 注入最新恢复包；clear/未初始化 no-op。"""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.paths = project_paths(self.root)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _init_with_requirement(self, name: str) -> None:
        init_project(self.root, name)
        add_requirement(self.paths, "ZCode 恢复要求：保持 API 兼容")

    def test_injects_for_startup_resume_and_compact(self) -> None:
        self._init_with_requirement("zcode-inject")
        for source in ("startup", "resume", "compact"):
            outcome = handle_zcode_hook_event(_session_start_payload(self.root, source))
            context = outcome.output["hookSpecificOutput"]["additionalContext"]
            self.assertEqual(outcome.output["hookSpecificOutput"]["hookEventName"], "SessionStart", source)
            self.assertIn("ZCode 恢复要求：保持 API 兼容", context, source)
            self.assertIn("Completion Gate", context, source)
            self.assertIn("R001", context, source)

    def test_clear_source_skips_injection(self) -> None:
        self._init_with_requirement("zcode-clear")
        outcome = handle_zcode_hook_event(_session_start_payload(self.root, "clear"))
        self.assertIsNone(outcome.output)

    def test_uninitialized_is_noop_and_creates_nothing(self) -> None:
        outcome = handle_zcode_hook_event(_session_start_payload(self.root, "startup"))
        self.assertIsNone(outcome.output)
        self.assertFalse((self.root / ".context-guard").exists())

    def test_recovery_packet_is_rebuilt_from_state_not_recovery_md(self) -> None:
        self._init_with_requirement("zcode-fresh-packet")
        stale = self.root / ".context-guard" / "recovery.md"
        stale.write_text("# 过期内容：不应该被注入\n", encoding="utf-8")
        outcome = handle_zcode_hook_event(_session_start_payload(self.root, "resume"))
        context = outcome.output["hookSpecificOutput"]["additionalContext"]
        self.assertIn("ZCode 恢复要求：保持 API 兼容", context)
        self.assertNotIn("过期内容", context)

    def test_project_root_comes_from_payload_cwd(self) -> None:
        self._init_with_requirement("zcode-cwd")
        other = tempfile.TemporaryDirectory()
        try:
            original_cwd = os.getcwd()
            os.chdir(other.name)
            try:
                outcome = handle_zcode_hook_event(_session_start_payload(self.root, "resume"))
            finally:
                os.chdir(original_cwd)
            self.assertIn("ZCode 恢复要求", outcome.output["hookSpecificOutput"]["additionalContext"])
        finally:
            other.cleanup()

    def test_huge_notebook_yields_bounded_packet(self) -> None:
        self._init_with_requirement("zcode-huge")
        notebook = self.root / ".context-guard" / "notebook.md"
        notebook.write_text("行\n" * 20000, encoding="utf-8")
        outcome = handle_zcode_hook_event(_session_start_payload(self.root, "resume"))
        context = outcome.output["hookSpecificOutput"]["additionalContext"]
        self.assertLess(len(context), 30000)


class ZCodeProtocolBoundaryTests(unittest.TestCase):
    """非法输入、不支持事件与 CLI 入口契约。"""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_run_command_rejects_invalid_json(self) -> None:
        code = run_zcode_hook_command(
            stdin=io.BytesIO(b"{broken"),
            stdout=io.StringIO(),
            stderr=io.StringIO(),
        )
        self.assertEqual(code, 1)

    def test_run_command_rejects_missing_event_name(self) -> None:
        payload = json.dumps({"cwd": str(self.root)}).encode("utf-8")
        code = run_zcode_hook_command(
            stdin=io.BytesIO(payload),
            stdout=io.StringIO(),
            stderr=io.StringIO(),
        )
        self.assertEqual(code, 1)

    def test_run_command_rejects_pre_compact_as_unsupported(self) -> None:
        # ZCode 没有 PreCompact 事件：必须明确拒绝，不得静默 no-op。
        payload = json.dumps({"hook_event_name": "PreCompact", "cwd": str(self.root)}).encode("utf-8")
        fake_out, fake_err = io.StringIO(), io.StringIO()
        code = run_zcode_hook_command(stdin=io.BytesIO(payload), stdout=fake_out, stderr=fake_err)
        self.assertEqual(code, 1)
        self.assertEqual(fake_out.getvalue(), "")
        self.assertIn("PreCompact", fake_err.getvalue())

    def test_run_command_writes_single_json_line_on_block(self) -> None:
        init_project(self.root, "zcode-cli-block")
        paths = project_paths(self.root)
        add_requirement(paths, "CLI 入口阻塞")
        payload = json.dumps(_stop_payload(self.root)).encode("utf-8")
        fake_out, fake_err = io.StringIO(), io.StringIO()
        code = run_zcode_hook_command(stdin=io.BytesIO(payload), stdout=fake_out, stderr=fake_err)
        self.assertEqual(code, 0)
        parsed = json.loads(fake_out.getvalue())
        self.assertEqual(parsed["decision"], "block")
        self.assertNotIn("continue", parsed)

    def test_run_command_writes_nothing_on_pass(self) -> None:
        init_project(self.root, "zcode-cli-pass")
        payload = json.dumps(_stop_payload(self.root)).encode("utf-8")
        fake_out, fake_err = io.StringIO(), io.StringIO()
        code = run_zcode_hook_command(stdin=io.BytesIO(payload), stdout=fake_out, stderr=fake_err)
        self.assertEqual(code, 0)
        self.assertEqual(fake_out.getvalue(), "")

    def test_repeated_calls_do_not_corrupt_state(self) -> None:
        init_project(self.root, "zcode-repeat")
        paths = project_paths(self.root)
        add_requirement(paths, "重复调用稳定性")
        before = paths.state.read_bytes()
        for _ in range(3):
            handle_zcode_hook_event(_stop_payload(self.root))
        self.assertEqual(paths.state.read_bytes(), before)
        blocked = handle_zcode_hook_event(_stop_payload(self.root))
        self.assertEqual(blocked.output["decision"], "block")

    def test_chinese_path_and_spaces(self) -> None:
        nested = Path(self.temp_dir.name) / "中文 项目 目录"
        nested.mkdir()
        init_project(nested, "中文路径项目")
        paths = project_paths(nested)
        add_requirement(paths, "中文路径下的要求")
        blocked = handle_zcode_hook_event(_stop_payload(nested))
        self.assertEqual(blocked.output["decision"], "block")
        self.assertIn("R001", blocked.output["reason"])
        add_evidence(paths, "R001", "中文路径测试通过", "success")
        update_requirement(paths, "R001", status="done")
        outcome = handle_zcode_hook_event(_stop_payload(nested))
        self.assertIsNone(outcome.output)


class ZCodePluginFilesTests(unittest.TestCase):
    """Plugin 分发文件与 wrapper 的结构契约。"""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_repo_plugin_files_match_protocol(self) -> None:
        manifest = json.loads((REPO_ROOT / ".zcode-plugin" / "plugin.json").read_text(encoding="utf-8"))
        self.assertRegex(manifest["name"], r"^[a-z0-9][a-z0-9._-]{0,127}$")
        self.assertEqual(manifest["hooks"], "hooks")

        hooks = json.loads((REPO_ROOT / "hooks" / "hooks.json").read_text(encoding="utf-8"))
        # ZCode 仅支持 7 事件且无 PreCompact：本插件只注册 SessionStart 与 Stop。
        self.assertEqual(set(hooks["hooks"]), {"SessionStart", "Stop"})
        session_start = hooks["hooks"]["SessionStart"][0]
        self.assertEqual(session_start["matcher"], SESSION_START_MATCHER)
        self.assertNotIn("matcher", hooks["hooks"]["Stop"][0])
        for event in ("SessionStart", "Stop"):
            handler = hooks["hooks"][event][0]["hooks"][0]
            self.assertEqual(handler["type"], "process")
            self.assertEqual(handler["command"], "python3")
            self.assertIn("${ZCODE_PLUGIN_ROOT}/hooks/zcode_hook.py", handler["args"][0])
            self.assertIsInstance(handler["timeoutMs"], int)
        marketplace = json.loads((REPO_ROOT / ".zcode-marketplace" / "marketplace.json").read_text(encoding="utf-8"))
        self.assertEqual(marketplace["plugins"][0]["source"], "./")

    def test_wrapper_subprocess_delegates_to_cli(self) -> None:
        init_project(self.root, "zcode-wrapper")
        paths = project_paths(self.root)
        add_requirement(paths, "wrapper 子进程阻塞")
        payload = json.dumps(_stop_payload(self.root)).encode("utf-8")
        proc = subprocess.run(
            [sys.executable, str(WRAPPER)],
            input=payload,
            capture_output=True,
            cwd=tempfile.gettempdir(),
            timeout=60,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr.decode("utf-8", errors="replace"))
        parsed = json.loads(proc.stdout.decode("utf-8"))
        self.assertEqual(parsed["decision"], "block")
        self.assertIn("R001", parsed["reason"])

    def test_wrapper_subprocess_injects_on_session_start(self) -> None:
        init_project(self.root, "zcode-wrapper-session")
        paths = project_paths(self.root)
        add_requirement(paths, "wrapper 注入要求")
        payload = json.dumps(_session_start_payload(self.root, "resume")).encode("utf-8")
        proc = subprocess.run(
            [sys.executable, str(WRAPPER)],
            input=payload,
            capture_output=True,
            cwd=tempfile.gettempdir(),
            timeout=60,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr.decode("utf-8", errors="replace"))
        parsed = json.loads(proc.stdout.decode("utf-8"))
        self.assertEqual(parsed["hookSpecificOutput"]["hookEventName"], "SessionStart")
        self.assertIn("wrapper 注入要求", parsed["hookSpecificOutput"]["additionalContext"])

    def test_status_reports_files_install_and_protocol(self) -> None:
        status = zcode_hook_status(REPO_ROOT)
        self.assertTrue(status["plugin_files"]["manifest_exists"])
        self.assertTrue(status["plugin_files"]["manifest_valid"])
        self.assertTrue(status["plugin_files"]["hooks_file_valid"])
        self.assertTrue(status["plugin_files"]["wrapper_exists"])
        self.assertTrue(status["plugin_files"]["events"]["SessionStart"]["configured"])
        self.assertTrue(status["plugin_files"]["events"]["Stop"]["configured"])
        self.assertFalse(status["protocol"]["pre_compact_supported"])
        self.assertEqual(status["protocol"]["stop_continuation_limit"], 3)
        self.assertTrue(status["protocol"]["session_start_e2e_verified"])
        self.assertTrue(status["protocol"]["stop_e2e_verified"])
        self.assertIsInstance(status["python3_on_path"], bool)
        self.assertIsInstance(installed_plugin_status()["installed"], bool)

    def test_status_handles_uninitialized_project(self) -> None:
        status = zcode_hook_status(self.root)
        self.assertFalse(status["project_initialized"])
        self.assertIsNone(status["protection_enabled"])
        files = plugin_files_status(self.root)
        self.assertFalse(files["manifest_exists"])
        self.assertFalse(files["hooks_file_exists"])
        self.assertFalse(files["wrapper_exists"])

    def test_cli_zcode_status_json(self) -> None:
        with contextlib.redirect_stdout(io.StringIO()) as fake_out:
            self.assertEqual(main(["--root", str(self.root), "zcode", "status", "--json"]), 0)
        parsed = json.loads(fake_out.getvalue())
        self.assertEqual(parsed["platform"], "zcode")
        self.assertIn("protocol", parsed)


if __name__ == "__main__":
    unittest.main()
