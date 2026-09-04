"""Claude Code 集成测试：配置管理（对称 Codex 套件）+ hook 协议复用。"""

from __future__ import annotations

import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

from context_guard_lite.cli import main
from context_guard_lite.contract import init_project, project_paths
from context_guard_lite.integrations.claude import (
    CLAUDE_HOOK_COMMAND,
    claude_hook_status,
    handle_claude_hook_event,
    hooks_settings_path,
)
from context_guard_lite.requirements import add_requirement


def _stop_payload(cwd: Path, stop_hook_active: bool = False) -> dict:
    return {
        "hook_event_name": "Stop",
        "cwd": str(cwd),
        "session_id": "s-1",
        "transcript_path": None,
        "stop_hook_active": stop_hook_active,
        "turn_id": "t-1",
        "last_assistant_message": None,
        "model": "claude-x",
        "permission_mode": "default",
    }


def _session_start_payload(cwd: Path, source: str) -> dict:
    return {
        "hook_event_name": "SessionStart",
        "cwd": str(cwd),
        "session_id": "s-1",
        "transcript_path": None,
        "source": source,
        "model": "claude-x",
        "permission_mode": "default",
    }


class ClaudeConfigTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.hooks_path = hooks_settings_path(self.root)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_install_creates_settings_with_three_hooks(self) -> None:
        init_project(self.root, "claude-demo")
        self.assertEqual(main(["--root", str(self.root), "claude", "install"]), 0)
        config = json.loads(self.hooks_path.read_text(encoding="utf-8"))
        hooks = config["hooks"]
        self.assertEqual(set(hooks), {"PreCompact", "SessionStart", "Stop"})
        self.assertEqual(hooks["PreCompact"][0]["matcher"], "manual|auto")
        self.assertEqual(hooks["SessionStart"][0]["matcher"], "resume|compact")
        self.assertNotIn("matcher", hooks["Stop"][0])
        handler = hooks["SessionStart"][0]["hooks"][0]
        # Claude handler 保持最小字段集合：type/command/timeout
        self.assertEqual(set(handler), {"type", "command", "timeout"})
        self.assertEqual(handler["command"], CLAUDE_HOOK_COMMAND)

    def test_install_preserves_user_settings_and_third_party_hooks(self) -> None:
        self.hooks_path.parent.mkdir(parents=True)
        user_settings = {
            "permissions": {"allow": ["Bash(ls:*)"]},
            "hooks": {
                "Stop": [{"hooks": [{"type": "command", "command": "my-own-notify"}]}],
                "SubagentStop": [{"hooks": [{"type": "command", "command": "sub-notify"}]}],
            },
        }
        self.hooks_path.write_text(json.dumps(user_settings), encoding="utf-8")
        self.assertEqual(main(["--root", str(self.root), "claude", "install"]), 0)
        merged = json.loads(self.hooks_path.read_text(encoding="utf-8"))
        # 用户自己的 permissions 与第三方 hook 原样保留
        self.assertEqual(merged["permissions"], user_settings["permissions"])
        self.assertEqual(
            merged["hooks"]["Stop"][0]["hooks"][0]["command"], "my-own-notify"
        )
        self.assertIn("SubagentStop", merged["hooks"])
        self.assertIn("memory-corridor claude hook", json.dumps(merged))

    def test_install_idempotent_and_uninstall_keeps_user_config(self) -> None:
        self.assertEqual(main(["--root", str(self.root), "claude", "install"]), 0)
        first = self.hooks_path.read_text(encoding="utf-8")
        self.assertEqual(main(["--root", str(self.root), "claude", "install"]), 0)
        self.assertEqual(first, self.hooks_path.read_text(encoding="utf-8"))

        self.assertEqual(main(["--root", str(self.root), "claude", "uninstall"]), 0)
        remaining = json.loads(self.hooks_path.read_text(encoding="utf-8"))
        self.assertNotIn(CLAUDE_HOOK_COMMAND, json.dumps(remaining))
        # 幂等卸载
        self.assertEqual(main(["--root", str(self.root), "claude", "uninstall"]), 0)

    def test_install_refuses_invalid_json_without_touching_it(self) -> None:
        self.hooks_path.parent.mkdir(parents=True)
        self.hooks_path.write_text("{broken", encoding="utf-8")
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(main(["--root", str(self.root), "claude", "install"]), 2)
        self.assertEqual(self.hooks_path.read_text(encoding="utf-8"), "{broken")

    def test_status_reports_configuration_and_drift(self) -> None:
        status = claude_hook_status(self.root)
        self.assertFalse(status["hooks_file_exists"])
        self.assertEqual(status["trust"], "unable to determine automatically")

        self.assertEqual(main(["--root", str(self.root), "claude", "install"]), 0)
        status = claude_hook_status(self.root)
        for event in ("PreCompact", "SessionStart", "Stop"):
            self.assertTrue(status["events"][event]["configured"], event)
            self.assertFalse(status["events"][event]["matcher_drifted"], event)

        config = json.loads(self.hooks_path.read_text(encoding="utf-8"))
        config["hooks"]["SessionStart"][0]["matcher"] = "resume"
        self.hooks_path.write_text(json.dumps(config), encoding="utf-8")
        drifted = claude_hook_status(self.root)
        self.assertTrue(drifted["events"]["SessionStart"]["matcher_drifted"])

        with contextlib.redirect_stdout(io.StringIO()) as fake_out:
            self.assertEqual(main(["--root", str(self.root), "claude", "status"]), 0)
        self.assertIn("偏离", fake_out.getvalue())

    def test_status_json_is_script_friendly(self) -> None:
        with contextlib.redirect_stdout(io.StringIO()) as fake_out:
            self.assertEqual(main(["--root", str(self.root), "claude", "status", "--json"]), 0)
        parsed = json.loads(fake_out.getvalue())
        self.assertEqual(parsed["platform"], "claude")
        self.assertIsInstance(parsed["command_on_path"], bool)


class ClaudeHookHandlingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.paths = project_paths(self.root)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_stop_block_and_loop_guard_share_codex_handler(self) -> None:
        init_project(self.root, "claude-stop")
        add_requirement(self.paths, "Claude 侧未完成项")
        blocked = handle_claude_hook_event(_stop_payload(self.root))
        self.assertEqual(blocked.output["decision"], "block")
        self.assertIn("R001", blocked.output["reason"])

        looping = handle_claude_hook_event(_stop_payload(self.root, stop_hook_active=True))
        self.assertTrue(looping.output["continue"])
        self.assertNotIn("decision", looping.output)
        self.assertIn("avoid a loop", looping.output["systemMessage"])

    def test_session_start_resume_injects_and_startup_skips(self) -> None:
        init_project(self.root, "claude-resume")
        add_requirement(self.paths, "Claude 侧恢复要求")
        injected = handle_claude_hook_event(_session_start_payload(self.root, "resume"))
        self.assertIn("Claude 侧恢复要求", injected.output["hookSpecificOutput"]["additionalContext"])
        skipped = handle_claude_hook_event(_session_start_payload(self.root, "startup"))
        self.assertNotIn("hookSpecificOutput", skipped.output)

    def test_cli_hook_entry_outputs_valid_json(self) -> None:
        init_project(self.root, "claude-cli")
        payload = json.dumps(_stop_payload(self.root)).encode("utf-8")
        original_stdin = sys.stdin
        sys.stdin = io.BytesIO(payload)
        try:
            with contextlib.redirect_stdout(io.StringIO()) as fake_out:
                with contextlib.redirect_stderr(io.StringIO()):
                    self.assertEqual(main(["claude", "hook"]), 0)
        finally:
            sys.stdin = original_stdin
        parsed = json.loads(fake_out.getvalue())
        self.assertEqual(parsed["decision"], "block")

    def test_uninitialized_stop_is_noop(self) -> None:
        outcome = handle_claude_hook_event(_stop_payload(self.root))
        self.assertTrue(outcome.output["continue"])
        self.assertFalse((self.root / ".context-guard").exists())


if __name__ == "__main__":
    unittest.main()
