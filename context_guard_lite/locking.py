"""跨平台状态锁与读改写事务。

为什么需要：``load_state`` → 修改 → ``save_state`` 是非原子的读改写序列。
``atomic_write`` 只保证「不会写出半个文件」，不保证「不会覆盖别人刚写的内容」。
当 AI hook、终端命令、编辑器脚本并发写同一个项目时，会发生丢失更新
（实测 30 个并发 ``requirements add`` 只剩 25~28 条，且 ID 会重号）。

设计约束（与项目现状一致）：
- 零运行时依赖：只用标准库 ``msvcrt`` / ``fcntl``；
- 锁文件独立于 state.json（``.context-guard/state.lock``），不影响原子替换写；
- 超时可控，超时抛 GuardError 而不是无限等待（hook 场景不能挂死）。
"""

from __future__ import annotations

import contextlib
import os
import time

from .contract import GuardError, ProjectPaths, load_state, save_state

LOCK_FILE_NAME = "state.lock"
LOCK_TIMEOUT_SECONDS = 10.0
_POLL_INTERVAL = 0.02

if os.name == "nt":
    import msvcrt

    def _try_lock(handle) -> bool:
        try:
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            return True
        except OSError:
            return False

    def _unlock(handle) -> None:
        with contextlib.suppress(OSError):
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
else:
    import fcntl

    def _try_lock(handle) -> bool:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError:
            return False

    def _unlock(handle) -> None:
        with contextlib.suppress(OSError):
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@contextlib.contextmanager
def state_lock(paths: ProjectPaths, timeout: float = LOCK_TIMEOUT_SECONDS):
    """独占持有项目状态锁；超时抛 GuardError 而不是无限等待。"""
    paths.data.mkdir(parents=True, exist_ok=True)
    lock_path = paths.data / LOCK_FILE_NAME
    deadline = time.monotonic() + timeout
    with open(lock_path, "a+b") as handle:
        while True:
            if _try_lock(handle):
                break
            if time.monotonic() >= deadline:
                raise GuardError(
                    f"获取状态锁超时（{timeout:.0f}s）：{lock_path}\n"
                    "可能有另一个 memory-corridor 进程正在写入；确认没有相关进程后可删除该锁文件。"
                )
            time.sleep(_POLL_INTERVAL)
        try:
            yield
        finally:
            _unlock(handle)


@contextlib.contextmanager
def state_transaction(paths: ProjectPaths, timeout: float = LOCK_TIMEOUT_SECONDS):
    """锁内 load → 修改 → save 的读改写事务。

    用法::

        with state_transaction(paths) as state:
            state["requirements"].append(...)

    块内抛异常时不落盘，state.json 保持修改前内容。
    """
    with state_lock(paths, timeout):
        state = load_state(paths)
        yield state
        save_state(paths, state)
