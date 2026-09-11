#!/usr/bin/env python3
"""ZCode Hook 最薄包装器：只做引导，不含任何业务规则。

ZCode Plugin 以 ``process`` 型 hook 调用本文件（``python3
${ZCODE_PLUGIN_ROOT}/hooks/zcode_hook.py``），无需 pip install。本脚本
把插件根目录加进 ``sys.path`` 后原样委托给 ``memory-corridor zcode
hook`` 的入口：stdin JSON 进、stdout JSON 出，诊断走 stderr；
requirement / evidence / recovery / gate 全部由包内既有模块处理。
"""

from __future__ import annotations

import os
import sys

PLUGIN_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

if PLUGIN_ROOT not in sys.path:
    sys.path.insert(0, PLUGIN_ROOT)

from context_guard_lite.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main(["zcode", "hook"]))
