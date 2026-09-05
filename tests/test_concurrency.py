"""并发安全回归测试：并发写入不得丢失更新或重号。

会起 20~30 个子进程，本地不到 2 秒，但在慢速 CI runner 上可能拖垮整个矩阵；
CI 里只在 ubuntu-latest × 3.12 这一格全量运行，其余格由
MC_SKIP_CONCURRENCY=1 跳过（见 .github/workflows/ci.yml）。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def _run(root: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "context_guard_lite", "--root", str(root), *args],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
    )


@unittest.skipIf(
    os.environ.get("MC_SKIP_CONCURRENCY") == "1",
    "CI 矩阵非并发格跳过慢速并发回归（MC_SKIP_CONCURRENCY=1）",
)
class ConcurrentWriteTests(unittest.TestCase):
    def test_parallel_requirement_add_loses_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _run(root, "init")
            count = 20
            with ThreadPoolExecutor(max_workers=count) as pool:
                list(pool.map(lambda i: _run(root, "requirements", "add", f"需求{i}"), range(count)))
            state = json.loads((root / ".context-guard" / "state.json").read_text(encoding="utf-8"))
            self.assertEqual(len(state["requirements"]), count)
            self.assertEqual(len({item["id"] for item in state["requirements"]}), count)

    def test_parallel_mixed_writes_keep_every_record(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _run(root, "init")
            _run(root, "requirements", "add", "基线")

            def job(index: int) -> None:
                if index % 3 == 0:
                    _run(root, "requirements", "add", f"需求{index}")
                elif index % 3 == 1:
                    _run(root, "evidence", "add", "--for", "R001", "--result", "success", "--summary", f"验证{index}")
                else:
                    _run(root, "note", "add", f"笔记{index}")

            with ThreadPoolExecutor(max_workers=18) as pool:
                list(pool.map(job, range(18)))
            state = json.loads((root / ".context-guard" / "state.json").read_text(encoding="utf-8"))
            self.assertEqual(len(state["requirements"]), 1 + 6)
            self.assertEqual(len(state["evidence"]), 6)
            self.assertEqual(len(state["notes"]), 6)
            self.assertEqual(len({item["id"] for item in state["evidence"]}), 6)
            self.assertEqual(len({item["id"] for item in state["notes"]}), 6)


if __name__ == "__main__":
    unittest.main()
