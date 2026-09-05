from __future__ import annotations

import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from context_guard_lite.cli import main
from context_guard_lite.contract import GuardError, init_project, load_state, project_paths, read_events
from context_guard_lite.evidence import add_evidence
from context_guard_lite.integrations.claude import handle_claude_hook_event
from context_guard_lite.integrations.codex import (
    ADDITIONAL_CONTEXT_LIMIT,
    HOOK_COMMAND,
    PRE_COMPACT_MATCHER,
    SESSION_START_MATCHER,
    handle_hook_event,
    hook_status,
    hooks_config_path,
)
from context_guard_lite.requirements import add_requirement, update_requirement


class _FakeTTY(io.BytesIO):
    def isatty(self) -> bool:
        return True


@contextlib.contextmanager
def _stdin_as(buffer):
    if isinstance(buffer, (bytes, bytearray)):
        buffer = io.BytesIO(buffer)
    original = sys.stdin
    sys.stdin = buffer
    try:
        yield
    finally:
        sys.stdin = original


def _pre_compact_payload(cwd: Path, trigger: str = "manual") -> dict:
    return {
        "hook_event_name": "PreCompact",
        "cwd": str(cwd),
        "session_id": "s-1",
        "transcript_path": None,
        "trigger": trigger,
        "turn_id": "t-1",
        "model": "gpt-5",
    }


def _session_start_payload(cwd: Path, source: str) -> dict:
    return {
        "hook_event_name": "SessionStart",
        "cwd": str(cwd),
        "session_id": "s-1",
        "transcript_path": None,
        "source": source,
        "model": "gpt-5",
        "permission_mode": "default",
    }


def _stop_payload(cwd: Path, stop_hook_active: bool = False) -> dict:
    return {
        "hook_event_name": "Stop",
        "cwd": str(cwd),
        "session_id": "s-1",
        "transcript_path": None,
        "stop_hook_active": stop_hook_active,
        "turn_id": "t-1",
        "last_assistant_message": None,
        "model": "gpt-5",
        "permission_mode": "default",
    }


class CodexHookHandlingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.paths = project_paths(self.root)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    # 场景 1：PreCompact + initialized → recovery.md 被刷新
    def test_pre_compact_initialized_refreshes_recovery_md(self) -> None:
        init_project(self.root, "compact-demo")
        add_requirement(self.paths, "压缩前应保留的要求")
        self.assertFalse(self.paths.recovery.exists())
        outcome = handle_hook_event(_pre_compact_payload(self.root))
        self.assertTrue(outcome.output["continue"])
        self.assertTrue(self.paths.recovery.exists())
        content = self.paths.recovery.read_text(encoding="utf-8")
        self.assertIn("压缩前应保留的要求", content)
        self.assertIn("compact-demo", content)

    # 场景 2：PreCompact + uninitialized → no-op，不创建 .context-guard
    def test_pre_compact_uninitialized_is_noop(self) -> None:
        outcome = handle_hook_event(_pre_compact_payload(self.root))
        self.assertTrue(outcome.output["continue"])
        self.assertIsNone(outcome.output.get("systemMessage"))
        self.assertFalse((self.root / ".context-guard").exists())

    # 场景 3/4：SessionStart source=resume / compact → additionalContext
    def test_session_start_resume_and_compact_inject_packet(self) -> None:
        for source in ("resume", "compact"):
            with self.subTest(source=source):
                project = self.root / source
                project.mkdir()
                paths = project_paths(project)
                init_project(project, f"resume-{source}")
                add_requirement(paths, "恢复时必须看到的要求")
                outcome = handle_hook_event(_session_start_payload(project, source))
                specific = outcome.output["hookSpecificOutput"]
                self.assertEqual(specific["hookEventName"], "SessionStart")
                self.assertIn("恢复时必须看到的要求", specific["additionalContext"])
                self.assertIn("Completion Gate", specific["additionalContext"])

    # 场景 5/6：startup / clear → 不注入旧状态
    def test_session_start_startup_and_clear_do_not_inject(self) -> None:
        init_project(self.root, "no-inject")
        add_requirement(self.paths, "不该被注入的要求")
        for source in ("startup", "clear"):
            with self.subTest(source=source):
                outcome = handle_hook_event(_session_start_payload(self.root, source))
                self.assertTrue(outcome.output["continue"])
                self.assertNotIn("hookSpecificOutput", outcome.output)
                self.assertNotIn("不该被注入的要求", json.dumps(outcome.output, ensure_ascii=False))

    def test_session_start_uninitialized_is_noop(self) -> None:
        outcome = handle_hook_event(_session_start_payload(self.root, "resume"))
        self.assertTrue(outcome.output["continue"])
        self.assertNotIn("hookSpecificOutput", outcome.output)
        self.assertFalse((self.root / ".context-guard").exists())

    # 场景 7：Stop + gate pass → 不 block
    def test_stop_gate_pass_continues(self) -> None:
        init_project(self.root, "pass")
        requirement = add_requirement(self.paths, "已完成并验证")
        add_evidence(self.paths, requirement["id"], "验证通过", "success")
        update_requirement(self.paths, requirement["id"], status="done")
        outcome = handle_hook_event(_stop_payload(self.root))
        self.assertTrue(outcome.output["continue"])
        self.assertNotIn("decision", outcome.output)

    # 场景 8：Stop + gate disabled → 不 block
    def test_stop_disabled_continues(self) -> None:
        init_project(self.root, "disabled")
        add_requirement(self.paths, "未完成但保护已关")
        main(["--root", str(self.root), "off"])
        outcome = handle_hook_event(_stop_payload(self.root))
        self.assertTrue(outcome.output["continue"])
        self.assertNotIn("decision", outcome.output)

    # 场景 9：Stop + uninitialized → 不 block，不创建目录
    def test_stop_uninitialized_continues(self) -> None:
        outcome = handle_hook_event(_stop_payload(self.root))
        self.assertTrue(outcome.output["continue"])
        self.assertNotIn("decision", outcome.output)
        self.assertFalse((self.root / ".context-guard").exists())

    # 场景 10：Stop + requirement open → block，含 Rxxx
    def test_stop_open_requirement_blocks(self) -> None:
        init_project(self.root, "blocked-open")
        add_requirement(self.paths, "还没做完的要求")
        outcome = handle_hook_event(_stop_payload(self.root))
        self.assertEqual(outcome.output["decision"], "block")
        reason = outcome.output["reason"]
        self.assertIn("Memory Corridor completion gate is blocked", reason)
        self.assertIn("R001", reason)
        self.assertIn("Resolve these blockers", reason)

    # 场景 11：Stop + done 但无当前 revision success evidence → block
    def test_stop_done_without_current_evidence_blocks(self) -> None:
        init_project(self.root, "blocked-no-evidence")
        requirement = add_requirement(self.paths, "标记了完成但没有证据")
        update_requirement(self.paths, requirement["id"], status="done")
        outcome = handle_hook_event(_stop_payload(self.root))
        self.assertEqual(outcome.output["decision"], "block")
        self.assertIn("R001", outcome.output["reason"])

    # 场景 12：Stop + 当前 revision 最新 evidence=failed → block
    def test_stop_latest_failed_evidence_blocks(self) -> None:
        init_project(self.root, "blocked-failed")
        requirement = add_requirement(self.paths, "最新一次验证失败")
        add_evidence(self.paths, requirement["id"], "验证失败", "failed")
        update_requirement(self.paths, requirement["id"], status="done")
        outcome = handle_hook_event(_stop_payload(self.root))
        self.assertEqual(outcome.output["decision"], "block")
        self.assertIn("R001", outcome.output["reason"])

    # 场景 13：Stop + 当前 revision 最新 evidence=success → pass
    def test_stop_latest_success_evidence_passes(self) -> None:
        init_project(self.root, "success")
        requirement = add_requirement(self.paths, "最新一次验证成功")
        add_evidence(self.paths, requirement["id"], "验证通过", "success")
        update_requirement(self.paths, requirement["id"], status="done")
        outcome = handle_hook_event(_stop_payload(self.root))
        self.assertTrue(outcome.output["continue"])
        self.assertNotIn("decision", outcome.output)

    # 场景 14：Stop + stop_hook_active=true → 不再产生无限 continuation block
    def test_stop_hook_active_does_not_block_again(self) -> None:
        init_project(self.root, "loop-guard")
        add_requirement(self.paths, "仍然未完成")
        outcome = handle_hook_event(_stop_payload(self.root, stop_hook_active=True))
        self.assertTrue(outcome.output["continue"])
        self.assertNotIn("decision", outcome.output)
        self.assertIn("blocked", outcome.output["systemMessage"])
        self.assertIn("R001", outcome.output["systemMessage"])

    # 大量阻塞项时 reason 保持精简，只列前 N 个 + 汇总提示
    def test_stop_blocker_reason_is_capped(self) -> None:
        init_project(self.root, "many-blockers")
        for index in range(1, 16):
            add_requirement(self.paths, f"未完成要求 {index:02d}")
        outcome = handle_hook_event(_stop_payload(self.root))
        self.assertEqual(outcome.output["decision"], "block")
        reason = outcome.output["reason"]
        self.assertIn("R010", reason)
        self.assertNotIn("R011", reason)
        self.assertIn("5 more blocked requirements not listed", reason)
        self.assertIn("gate check", reason)

    def test_stop_blocker_reason_within_cap_lists_all(self) -> None:
        init_project(self.root, "few-blockers")
        for index in range(1, 4):
            add_requirement(self.paths, f"未完成要求 {index}")
        outcome = handle_hook_event(_stop_payload(self.root))
        reason = outcome.output["reason"]
        self.assertIn("R003", reason)
        self.assertNotIn("more blocked requirements", reason)

    # 状态损坏：不得把“无法判断”当作 pass
    def test_stop_corrupt_state_is_not_treated_as_pass(self) -> None:
        init_project(self.root, "corrupt")
        self.paths.state.write_text("{broken json", encoding="utf-8")
        outcome = handle_hook_event(_stop_payload(self.root, stop_hook_active=False))
        self.assertEqual(outcome.output["decision"], "block")
        self.assertIn("could not be read", outcome.output["reason"])
        second = handle_hook_event(_stop_payload(self.root, stop_hook_active=True))
        self.assertTrue(second.output["continue"])
        self.assertNotIn("decision", second.output)
        self.assertIn("unreadable", second.output["systemMessage"])

    def test_session_start_corrupt_state_does_not_fake_packet(self) -> None:
        init_project(self.root, "corrupt-resume")
        self.paths.state.write_text("not json at all", encoding="utf-8")
        outcome = handle_hook_event(_session_start_payload(self.root, "resume"))
        self.assertTrue(outcome.output["continue"])
        self.assertNotIn("hookSpecificOutput", outcome.output)
        self.assertIn("systemMessage", outcome.output)

    def test_pre_compact_corrupt_state_does_not_raise_or_overwrite(self) -> None:
        init_project(self.root, "corrupt-compact")
        self.paths.state.write_text("[", encoding="utf-8")
        outcome = handle_hook_event(_pre_compact_payload(self.root))
        self.assertTrue(outcome.output["continue"])
        self.assertFalse(self.paths.recovery.exists())
        self.assertIn("systemMessage", outcome.output)


class CodexHookProtocolTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    # 场景 15：stdin 非法 JSON → 明确错误，stdout 不产生伪合法 Hook 结果
    def test_invalid_stdin_json_fails_cleanly(self) -> None:
        with _stdin_as(b"{not json"):
            with contextlib.redirect_stdout(io.StringIO()) as fake_out:
                with contextlib.redirect_stderr(io.StringIO()) as fake_err:
                    code = main(["codex", "hook"])
        self.assertEqual(code, 1)
        self.assertEqual(fake_out.getvalue(), "")
        self.assertIn("不是合法 JSON", fake_err.getvalue())

    def test_missing_hook_event_name_fails_cleanly(self) -> None:
        with _stdin_as(b'{"cwd": "/tmp"}'):
            with contextlib.redirect_stdout(io.StringIO()) as fake_out:
                with contextlib.redirect_stderr(io.StringIO()) as fake_err:
                    code = main(["codex", "hook"])
        self.assertEqual(code, 1)
        self.assertEqual(fake_out.getvalue(), "")
        self.assertIn("hook_event_name", fake_err.getvalue())

    def test_unknown_event_fails_cleanly(self) -> None:
        payload = json.dumps({"hook_event_name": "SessionEnd", "cwd": str(self.root)}).encode("utf-8")
        with _stdin_as(payload):
            with contextlib.redirect_stdout(io.StringIO()) as fake_out:
                with contextlib.redirect_stderr(io.StringIO()) as fake_err:
                    code = main(["codex", "hook"])
        self.assertEqual(code, 1)
        self.assertEqual(fake_out.getvalue(), "")
        self.assertIn("不支持的 hook_event_name", fake_err.getvalue())

    def test_stop_block_via_cli_stdout_is_valid_json(self) -> None:
        init_project(self.root, "cli-stop")
        add_requirement(project_paths(self.root), "CLI 链路验证")
        payload = json.dumps(_stop_payload(self.root)).encode("utf-8")
        with _stdin_as(payload):
            with contextlib.redirect_stdout(io.StringIO()) as fake_out:
                with contextlib.redirect_stderr(io.StringIO()):
                    code = main(["codex", "hook"])
        self.assertEqual(code, 0)
        parsed = json.loads(fake_out.getvalue())
        self.assertEqual(parsed["decision"], "block")
        self.assertIn("R001", parsed["reason"])


class CodexConfigTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.hooks_path = hooks_config_path(self.root)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _install_via_cli(self) -> None:
        self.assertEqual(main(["--root", str(self.root), "codex", "install"]), 0)

    def _load_config(self) -> dict:
        return json.loads(self.hooks_path.read_text(encoding="utf-8"))

    # 场景 16：install 到没有 .codex/hooks.json 的项目 → 正确生成
    def test_install_creates_file_with_three_hooks(self) -> None:
        self._install_via_cli()
        self.assertTrue(self.hooks_path.exists())
        hooks = self._load_config()["hooks"]
        self.assertEqual(set(hooks), {"PreCompact", "SessionStart", "Stop"})
        self.assertEqual(hooks["PreCompact"][0]["matcher"], PRE_COMPACT_MATCHER)
        self.assertEqual(hooks["SessionStart"][0]["matcher"], SESSION_START_MATCHER)
        self.assertNotIn("matcher", hooks["Stop"][0])
        stop_handler = hooks["Stop"][0]["hooks"][0]
        self.assertEqual(stop_handler["type"], "command")
        self.assertEqual(stop_handler["command"], HOOK_COMMAND)
        session_handler = hooks["SessionStart"][0]["hooks"][0]
        self.assertEqual(session_handler["additionalContextLimit"], ADDITIONAL_CONTEXT_LIMIT)
        self.assertNotIn("additionalContextLimit", stop_handler)

    # 场景 17：install 到已有第三方 hooks 的项目 → 第三方配置完整保留
    def test_install_preserves_third_party_hooks(self) -> None:
        third_party = {
            "description": "my own hooks",
            "hooks": {
                "PreCompact": [
                    {"matcher": "manual", "hooks": [{"type": "command", "command": "python backup.py"}]},
                ],
                "Stop": [
                    {"hooks": [{"type": "command", "command": "notify.exe --sound ding"}]},
                ],
                "SessionEnd": [
                    {"hooks": [{"type": "command", "command": "cleanup.sh"}]},
                ],
            },
        }
        self.hooks_path.parent.mkdir(parents=True)
        self.hooks_path.write_text(json.dumps(third_party, ensure_ascii=False, indent=2), encoding="utf-8")
        self._install_via_cli()
        merged = self._load_config()
        self.assertEqual(merged["description"], "my own hooks")
        self.assertEqual(
            merged["hooks"]["PreCompact"][0]["hooks"][0]["command"], "python backup.py"
        )
        self.assertEqual(
            merged["hooks"]["Stop"][0]["hooks"][0]["command"], "notify.exe --sound ding"
        )
        self.assertEqual(merged["hooks"]["SessionEnd"], third_party["hooks"]["SessionEnd"])
        backup = Path(str(self.hooks_path) + ".bak")
        self.assertTrue(backup.exists())
        self.assertEqual(json.loads(backup.read_text(encoding="utf-8")), third_party)

    # 场景 18：重复 install → 不重复添加
    def test_install_is_idempotent(self) -> None:
        self._install_via_cli()
        first = self._load_config()
        self._install_via_cli()
        second = self._load_config()
        self.assertEqual(first, second)
        for event in ("PreCompact", "SessionStart", "Stop"):
            handlers = [
                handler
                for group in first["hooks"][event]
                for handler in group["hooks"]
                if handler.get("command") == HOOK_COMMAND
            ]
            self.assertEqual(len(handlers), 1, event)

    # 场景 19：uninstall → 只移除 Memory Corridor Hook
    def test_uninstall_removes_only_memory_corridor_hooks(self) -> None:
        third_party = {
            "hooks": {
                "Stop": [
                    {"hooks": [{"type": "command", "command": "notify.exe"}]},
                ],
            },
        }
        self.hooks_path.parent.mkdir(parents=True)
        self.hooks_path.write_text(json.dumps(third_party), encoding="utf-8")
        self._install_via_cli()
        self.assertEqual(main(["--root", str(self.root), "codex", "uninstall"]), 0)
        config = self._load_config()
        self.assertEqual(config["hooks"]["Stop"], third_party["hooks"]["Stop"])
        dumped = json.dumps(config)
        self.assertNotIn(HOOK_COMMAND, dumped)
        for handler in config["hooks"]["Stop"][0]["hooks"]:
            self.assertEqual(handler["command"], "notify.exe")

    def test_uninstall_is_idempotent_and_handles_absent_file(self) -> None:
        self.assertEqual(main(["--root", str(self.root), "codex", "uninstall"]), 0)
        self._install_via_cli()
        self.assertEqual(main(["--root", str(self.root), "codex", "uninstall"]), 0)
        self.assertNotIn(HOOK_COMMAND, self.hooks_path.read_text(encoding="utf-8"))
        self.assertEqual(main(["--root", str(self.root), "codex", "uninstall"]), 0)

    def test_install_refuses_to_touch_invalid_json(self) -> None:
        self.hooks_path.parent.mkdir(parents=True)
        self.hooks_path.write_text("{broken", encoding="utf-8")
        with contextlib.redirect_stderr(io.StringIO()):
            code = main(["--root", str(self.root), "codex", "install"])
        self.assertEqual(code, 2)
        self.assertEqual(self.hooks_path.read_text(encoding="utf-8"), "{broken")
        self.assertFalse(Path(str(self.hooks_path) + ".bak").exists())

    def test_install_tolerates_utf8_bom_from_windows_editors(self) -> None:
        self.hooks_path.parent.mkdir(parents=True)
        self.hooks_path.write_bytes(b"\xef\xbb\xbf" + json.dumps({"hooks": {}}).encode("utf-8"))
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main(["--root", str(self.root), "codex", "install"]), 0)
        config = self._load_config()
        self.assertEqual(set(config["hooks"]), {"PreCompact", "SessionStart", "Stop"})
        # 读回的文件本身不带 BOM
        self.assertFalse(self.hooks_path.read_bytes().startswith(b"\xef\xbb\xbf"))

    def test_status_reports_configuration(self) -> None:
        status = hook_status(self.root)
        self.assertFalse(status["hooks_file_exists"])
        self.assertFalse(status["project_initialized"])
        self.assertEqual(status["trust"], "unable to determine automatically")
        self._install_via_cli()
        status = hook_status(self.root)
        self.assertTrue(status["hooks_file_exists"])
        self.assertTrue(status["hooks_file_valid"])
        for event in ("PreCompact", "SessionStart", "Stop"):
            self.assertTrue(status["events"][event]["configured"], event)
        self.assertFalse(status["events"]["Stop"]["third_party_present"])

    def test_status_json_output_is_script_friendly(self) -> None:
        import contextlib as _contextlib
        import io as _io

        self._install_via_cli()
        with _contextlib.redirect_stdout(_io.StringIO()) as fake_out:
            self.assertEqual(main(["--root", str(self.root), "codex", "status", "--json"]), 0)
        parsed = json.loads(fake_out.getvalue())
        self.assertTrue(parsed["hooks_file_exists"])
        self.assertTrue(parsed["hooks_file_valid"])
        self.assertEqual(parsed["trust"], "unable to determine automatically")
        self.assertIsInstance(parsed["command_on_path"], bool)
        for event in ("PreCompact", "SessionStart", "Stop"):
            self.assertIn(event, parsed["events"])

    def test_status_detects_matcher_drift(self) -> None:
        self._install_via_cli()
        status = hook_status(self.root)
        for event in ("PreCompact", "SessionStart", "Stop"):
            self.assertFalse(status["events"][event]["matcher_drifted"], event)
        # 手动改动 SessionStart 的 matcher → 检出漂移；Stop 无预期 matcher，不参与漂移检测
        config = self._load_config()
        config["hooks"]["SessionStart"][0]["matcher"] = "resume"
        self.hooks_path.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
        drifted = hook_status(self.root)
        self.assertTrue(drifted["events"]["SessionStart"]["matcher_drifted"])
        self.assertEqual(drifted["events"]["SessionStart"]["matcher"], "resume")
        self.assertEqual(drifted["events"]["SessionStart"]["matcher_expected"], "resume|compact")
        self.assertFalse(drifted["events"]["Stop"]["matcher_drifted"])
        self.assertFalse(drifted["events"]["PreCompact"]["matcher_drifted"])
        # CLI status 输出应包含警告
        import contextlib as _contextlib
        import io as _io

        with _contextlib.redirect_stdout(_io.StringIO()) as fake_out:
            self.assertEqual(main(["--root", str(self.root), "codex", "status"]), 0)
        self.assertIn("偏离", fake_out.getvalue())

    # 场景 20/21：不依赖 bash/PowerShell 路径语法；含空格路径正常
    def test_paths_with_spaces_work_without_shell_syntax(self) -> None:
        spaced = Path(tempfile.mkdtemp(prefix="memory corridor 空格 "))
        try:
            init_project(spaced, "spaced-project")
            self.assertEqual(main(["--root", str(spaced), "codex", "install"]), 0)
            outcome = handle_hook_event(_pre_compact_payload(spaced))
            self.assertTrue(outcome.output["continue"])
            paths = project_paths(spaced)
            self.assertTrue(paths.recovery.exists())
            # 空账本自 v2.8.0 起为 idle 放行；补一条未完成 requirement 让 Stop 处于应阻塞状态。
            add_requirement(paths, "未完成的要求")
            stop = handle_hook_event(_stop_payload(spaced))
            self.assertEqual(stop.output["decision"], "block")
            config = json.loads(hooks_config_path(spaced).read_text(encoding="utf-8"))
            command = config["hooks"]["Stop"][0]["hooks"][0]["command"]
            self.assertEqual(command, HOOK_COMMAND)
            self.assertNotIn("sh -c", command)
            self.assertNotIn("powershell", command.lower())
        finally:
            import shutil as _shutil

            _shutil.rmtree(spaced, ignore_errors=True)

    def test_uninitialized_hook_via_full_real_payload_shape(self) -> None:
        # 使用与官方 schema 相同的完整 required 字段集合验证真实 payload 兼容。
        payload = {
            "hook_event_name": "Stop",
            "cwd": str(self.root),
            "session_id": "a1b2c3",
            "transcript_path": None,
            "stop_hook_active": False,
            "turn_id": "turn-9",
            "last_assistant_message": "I think I am done.",
            "model": "gpt-5.2",
            "permission_mode": "acceptEdits",
        }
        outcome = handle_hook_event(payload)
        self.assertTrue(outcome.output["continue"])
        self.assertNotIn("decision", outcome.output)

    def test_load_state_guard_error_is_the_base_for_hook_conservatism(self) -> None:
        with self.assertRaises(GuardError):
            load_state(project_paths(self.root))

    def test_hook_command_rejects_tty_stdin(self) -> None:
        with _stdin_as(_FakeTTY(b"")):
            with contextlib.redirect_stdout(io.StringIO()) as fake_out:
                with contextlib.redirect_stderr(io.StringIO()) as fake_err:
                    code = main(["codex", "hook"])
        self.assertEqual(code, 1)
        self.assertEqual(fake_out.getvalue(), "")
        self.assertIn("stdin", fake_err.getvalue())

    def test_install_warns_when_command_missing_from_path(self) -> None:
        self.hooks_path.parent.mkdir(parents=True)
        with mock.patch("shutil.which", return_value=None):
            with contextlib.redirect_stdout(io.StringIO()) as fake_out:
                self.assertEqual(main(["--root", str(self.root), "codex", "install"]), 0)
        self.assertIn("警告", fake_out.getvalue())
        self.assertIn("PATH", fake_out.getvalue())
        # 命令在 PATH 上时不产生警告
        with mock.patch("shutil.which", return_value="/usr/local/bin/memory-corridor"):
            with contextlib.redirect_stdout(io.StringIO()) as fake_out:
                self.assertEqual(main(["--root", str(self.root), "codex", "install"]), 0)
        self.assertNotIn("警告", fake_out.getvalue())


class StopIdleAndObservabilityTests(unittest.TestCase):
    """v2.8.0：空账本 idle 放行引导 + hook 触发事件可观测（hook.* 事件）。"""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.paths = project_paths(self.root)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _stop_events(self) -> list[dict]:
        return read_events(self.paths, event_type="hook.stop")

    # 空账本：Stop 不再阻塞，放行并附引导文案
    def test_stop_idle_continues_with_onboarding_hint(self) -> None:
        init_project(self.root, "idle-demo")
        outcome = handle_hook_event(_stop_payload(self.root))
        self.assertTrue(outcome.output["continue"])
        self.assertNotIn("decision", outcome.output)
        self.assertIn("requirements add", outcome.output["systemMessage"])
        self.assertIn("ledger is empty", outcome.output["systemMessage"])

    # Stop 触发写 hook.stop 事件，decision/gate_status/blocking_count 可查
    def test_stop_blocked_records_hook_stop_event(self) -> None:
        init_project(self.root, "observe-block")
        add_requirement(self.paths, "未完成的要求")
        handle_hook_event(_stop_payload(self.root))
        events = self._stop_events()
        self.assertEqual(len(events), 1)
        event = events[0]
        self.assertEqual(event["platform"], "codex")
        self.assertEqual(event["decision"], "block")
        self.assertEqual(event["gate_status"], "blocked")
        self.assertEqual(event["blocking_count"], 1)
        self.assertFalse(event["stop_hook_active"])

    # 防循环放行也如实记录 stop_hook_active=true
    def test_stop_hook_active_records_flag(self) -> None:
        init_project(self.root, "observe-loop")
        add_requirement(self.paths, "未完成的要求")
        handle_hook_event(_stop_payload(self.root, stop_hook_active=True))
        event = self._stop_events()[0]
        self.assertEqual(event["decision"], "allow")
        self.assertTrue(event["stop_hook_active"])

    # 空账本 idle 放行也留痕（gate_status=idle）
    def test_stop_idle_records_idle_status(self) -> None:
        init_project(self.root, "observe-idle")
        handle_hook_event(_stop_payload(self.root))
        event = self._stop_events()[0]
        self.assertEqual(event["decision"], "allow")
        self.assertEqual(event["gate_status"], "idle")
        self.assertEqual(event["blocking_count"], 0)

    # Claude 平台经独立入口触发，事件 platform 如实记为 claude
    def test_claude_platform_recorded(self) -> None:
        init_project(self.root, "observe-claude")
        add_requirement(self.paths, "未完成的要求")
        handle_claude_hook_event(_stop_payload(self.root))
        event = self._stop_events()[0]
        self.assertEqual(event["platform"], "claude")
        self.assertEqual(event["decision"], "block")

    # 未初始化项目保持 no-op 承诺：不落任何文件
    def test_uninitialized_stop_records_nothing(self) -> None:
        outcome = handle_hook_event(_stop_payload(self.root))
        self.assertTrue(outcome.output["continue"])
        self.assertFalse((self.root / ".context-guard").exists())

    # PreCompact / SessionStart 触发同样留痕
    def test_pre_compact_records_event(self) -> None:
        init_project(self.root, "observe-compact")
        add_requirement(self.paths, "压缩前应保留的要求")
        handle_hook_event(_pre_compact_payload(self.root))
        events = read_events(self.paths, event_type="hook.pre_compact")
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["platform"], "codex")
        self.assertEqual(events[0]["result"], "refreshed")

    def test_session_start_records_inject_and_skip(self) -> None:
        init_project(self.root, "observe-session")
        add_requirement(self.paths, "恢复包应包含的要求")
        handle_hook_event(_session_start_payload(self.root, "startup"))
        handle_hook_event(_session_start_payload(self.root, "resume"))
        events = read_events(self.paths, event_type="hook.session_start")
        self.assertEqual([event["result"] for event in events], ["skipped", "injected"])
        self.assertEqual([event["source"] for event in events], ["startup", "resume"])


if __name__ == "__main__":
    unittest.main()
