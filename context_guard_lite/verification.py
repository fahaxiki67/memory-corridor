"""执行验证（execution evidence）与工作树指纹：strict 验收的最小实现。

为什么需要：``evidence add`` 的 result/command/summary 都是手工声明的，
门禁只看 requirement revision + result==success，无法区分「我真的跑过」
和「我声明我跑过」。本模块提供可选的 strict 验收：把一条真实执行的记录
（argv、cwd、起止时间、退出码、超时、日志与日志 hash、绑定的 requirement
revision、执行时的工作树指纹）写进 evidence，门禁在 strict 模式下只认
「执行过、退出码为零、日志在且未被改、代码没变」的 evidence。

边界（防误报，不防篡改）：
- 工作树指纹覆盖 HEAD + 未提交修改（``git diff HEAD``）+ 未跟踪文件内容，
  不是只绑 HEAD；指纹刻意排除 .context-guard/（日志与账本自身在其中，
  纳入指纹会造成「记录证据 → 指纹变化 → 证据过期」的循环失效）；
- 同权限用户仍可改文件、改账本、重放命令，本机制只防「无意的不一致」
  （改了代码还引用旧结论、日志丢了还说验证过），不承诺防篡改；
- 命令以 argv 列表直接执行（不经 shell），必须有超时；是否允许某条命令
  由调用方（人与当轮授权）决定，本工具不做白名单。
"""

from __future__ import annotations

import hashlib
import shlex
import subprocess
from pathlib import Path

from .contract import (
    EVIDENCE_RESULTS,
    GuardError,
    ProjectPaths,
    append_event,
    append_notebook,
    utc_now,
)
from .locking import state_transaction
from .requirements import get_requirement

DEFAULT_TIMEOUT_SECONDS = 600.0
# 轻量上限：单文件指纹最多哈希前 1MB；未跟踪文件超过 2000 个时按排序截断
# 并在指纹载荷中留下截断标记（截断是确定性的：按路径排序，集合变化仍会改变指纹）。
_FINGERPRINT_FILE_CAP = 1_000_000
_FINGERPRINT_FILE_LIMIT = 2000
# 日志轻量上限：stdout/stderr 各自截断到 512KB，超长部分以说明行代替。
_LOG_STREAM_CAP = 512_000
LOGS_DIR_NAME = "logs"


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def worktree_fingerprint(root: Path) -> str | None:
    """计算项目工作树指纹；非 git 工作树返回 None（调用方按「不可验证」处理）。

    载荷 = HEAD（或 unborn 标记）+ 未提交修改（含暂存与工作区、二进制内容）
    + 未跟踪文件内容 hash（排除 gitignore，因此 .context-guard/ 日志不在内）。
    只要不只看 HEAD，改一行未提交代码就会改变指纹；日志/账本自身变化不会。
    """
    root = str(root)

    def _git(*args: str, timeout: float = 30.0) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", "-C", root, *args],
            capture_output=True,
            text=True,
            errors="replace",
            timeout=timeout,
            check=False,
        )

    try:
        inside = _git("rev-parse", "--is-inside-work-tree")
        if inside.returncode != 0 or inside.stdout.strip() != "true":
            return None
        parts: list[str] = []
        head = _git("rev-parse", "HEAD")
        if head.returncode == 0:
            parts.append(f"HEAD={head.stdout.strip()}")
            diff = _git("diff", "--binary", "HEAD")
            parts.append(f"diff={sha256_hex(diff.stdout.encode('utf-8', errors='replace'))}")
        else:
            # 未出生分支（还没有任何提交）：没有 HEAD 可绑，全部文件按未跟踪覆盖。
            parts.append("HEAD=unborn")
        others = _git("ls-files", "--others", "--exclude-standard", "-z")
        entries: list[str] = []
        for name in others.stdout.split("\0"):
            if not name:
                continue
            file_path = Path(root) / name
            try:
                if not file_path.is_file():
                    continue
                data = file_path.read_bytes()
            except OSError:
                entries.append(f"{name}:unreadable")
                continue
            capped = data[:_FINGERPRINT_FILE_CAP]
            suffix = "" if len(data) <= _FINGERPRINT_FILE_CAP else "+capped"
            entries.append(f"{name}:{sha256_hex(capped)}{suffix}")
        entries.sort()
        if len(entries) > _FINGERPRINT_FILE_LIMIT:
            entries = entries[:_FINGERPRINT_FILE_LIMIT]
            entries.append(f"truncated>{_FINGERPRINT_FILE_LIMIT}")
        parts.append("untracked:\n" + "\n".join(entries))
        return sha256_hex("\n".join(parts).encode("utf-8", errors="replace"))
    except (OSError, subprocess.TimeoutExpired):
        # git 缺失/超时等无法计算的场景返回 None，绝不编造一个稳定值。
        return None


def _truncate(text: str, cap: int) -> str:
    if len(text) <= cap:
        return text
    return text[:cap] + f"\n…（超过 {cap} 字符已截断；完整输出以命令原始环境为准）"


def _run_argv(argv: list[str], cwd: Path, timeout: float) -> dict:
    """以 argv + 超时执行命令（不经 shell），返回执行记录字段。"""
    started_at = utc_now()
    timed_out = False
    launch_error: str | None = None
    stdout = ""
    stderr = ""
    exit_code: int | None = None
    try:
        completed = subprocess.run(
            argv,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            errors="replace",
            timeout=timeout,
            check=False,
        )
        exit_code = completed.returncode
        stdout = completed.stdout or ""
        stderr = completed.stderr or ""
    except subprocess.TimeoutExpired as exc:
        timed_out = True
        stdout = (exc.stdout or "").decode("utf-8", errors="replace") if isinstance(exc.stdout, bytes) else (exc.stdout or "")
        stderr = (exc.stderr or "").decode("utf-8", errors="replace") if isinstance(exc.stderr, bytes) else (exc.stderr or "")
    except (OSError, ValueError) as exc:
        # 命令无法启动（不存在/无权限等）按 failed 记录，不假装执行成功。
        launch_error = f"{type(exc).__name__}: {exc}"
    return {
        "argv": list(argv),
        "cwd": str(cwd),
        "started_at": started_at,
        "finished_at": utc_now(),
        "timeout_seconds": timeout,
        "timed_out": timed_out,
        "launch_error": launch_error,
        "exit_code": exit_code,
        "stdout": _truncate(stdout, _LOG_STREAM_CAP),
        "stderr": _truncate(stderr, _LOG_STREAM_CAP),
    }


def _log_content(record: dict) -> str:
    lines = [
        f"command: {shlex.join(record['argv'])}",
        f"cwd: {record['cwd']}",
        f"started_at: {record['started_at']}",
        f"finished_at: {record['finished_at']}",
        f"timeout_seconds: {record['timeout_seconds']}",
    ]
    if record["timed_out"]:
        lines.append("exit_code: timeout（超过时限被终止）")
    elif record["launch_error"]:
        lines.append(f"exit_code: launch_error（{record['launch_error']}）")
    else:
        lines.append(f"exit_code: {record['exit_code']}")
    lines.append("--- stdout ---")
    lines.append(record["stdout"])
    lines.append("--- stderr ---")
    lines.append(record["stderr"])
    return "\n".join(lines) + "\n"


def verify_requirement(
    paths: ProjectPaths,
    requirement_id: str,
    summary: str,
    argv: list[str],
    *,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    target: str | None = None,
) -> dict:
    """真实执行验证命令并记录 execution evidence；result 由实际退出码推导。

    时序刻意安排为：先跑命令（不持状态锁，锁内不能等长命令）→ 锁内分配
    evidence id → 先落日志再提交状态（状态提交时日志必然已在磁盘上，
    「日志缺失」只会因人为删除出现，从而能被门禁如实阻塞）。
    """
    clean_summary = " ".join(str(summary).split())
    if not clean_summary:
        raise GuardError("evidence summary 不能为空")
    argv = [str(item) for item in argv]
    if not argv or not any(arg.strip() for arg in argv):
        raise GuardError("未提供要执行的验证命令（argv 不能为空；用 -- 分隔命令与选项）")
    if timeout <= 0:
        raise GuardError(f"timeout 必须是正数（收到 {timeout!r}）")

    # 指纹取执行前的代码状态：验证结论只能对应「跑的时候」的代码。
    fingerprint = worktree_fingerprint(paths.root)
    record = _run_argv(argv, paths.root, timeout)
    succeeded = (not record["timed_out"]) and record["launch_error"] is None and record["exit_code"] == 0
    result = "success" if succeeded else "failed"

    with state_transaction(paths) as state:
        requirement = get_requirement(state, requirement_id)
        evidence_id = f"E{len(state['evidence']) + 1:03d}"
        log_relative = f"{LOGS_DIR_NAME}/{evidence_id}.log"
        log_path = paths.data / log_relative
        log_content = _log_content(record)
        from .contract import atomic_write  # 函数内导入与其他模块风格一致

        atomic_write(log_path, log_content)
        evidence = {
            "id": evidence_id,
            "requirement_id": requirement["id"],
            "requirement_revision": requirement["revision"],
            "result": result,
            "summary": clean_summary,
            "target": " ".join(str(target).split()) if target else None,
            "command": shlex.join(argv),
            "created_at": utc_now(),
            "verification": {
                "mode": "execution",
                "argv": record["argv"],
                "cwd": record["cwd"],
                "started_at": record["started_at"],
                "finished_at": record["finished_at"],
                "timeout_seconds": record["timeout_seconds"],
                "timed_out": record["timed_out"],
                "launch_error": record["launch_error"],
                "exit_code": record["exit_code"],
                "log_file": log_relative,
                "log_sha256": sha256_hex(log_content.encode("utf-8")),
                "requirement_revision": requirement["revision"],
                "worktree_fingerprint": fingerprint,
            },
        }
        state["evidence"].append(evidence)
        requirement = dict(requirement)

    append_event(
        paths,
        "evidence.verify",
        {
            "id": evidence_id,
            "requirement_id": requirement["id"],
            "requirement_revision": requirement["revision"],
            "result": result,
            "exit_code": record["exit_code"],
            "timed_out": record["timed_out"],
        },
    )
    details = [
        f"绑定：{requirement['id']} v{requirement['revision']}",
        f"结果：{result}（exit_code={record['exit_code']}{'，超时' if record['timed_out'] else ''}）",
        f"摘要：{clean_summary}",
        f"命令：{shlex.join(argv)}",
    ]
    append_notebook(paths, f"执行验证 evidence {evidence_id}", details)
    return evidence


def execution_stale_reasons(evidence: dict, paths: ProjectPaths) -> list[str]:
    """strict 门禁用：执行证据与当前现实不一致的原因（空列表 = 仍然有效）。"""
    verification = evidence.get("verification") or {}
    reasons: list[str] = []
    if verification.get("timed_out"):
        reasons.append(f"执行超时（时限 {verification.get('timeout_seconds')}s）")
    exit_code = verification.get("exit_code")
    if exit_code != 0:
        reasons.append(f"记录的退出码非零（exit_code={exit_code}）")
    if verification.get("launch_error"):
        reasons.append(f"命令未能启动（{verification['launch_error']}）")

    log_relative = verification.get("log_file")
    log_sha = verification.get("log_sha256")
    if not log_relative or not log_sha:
        reasons.append("验证日志记录不完整（缺日志路径或 hash）")
    else:
        log_path = paths.data / log_relative
        if not log_path.is_file():
            reasons.append(f"验证日志缺失（{log_relative}）：日志不在的证据不能算执行验证")
        else:
            try:
                actual_sha = sha256_hex(log_path.read_bytes())
            except OSError as exc:
                reasons.append(f"验证日志无法读取（{exc}）")
            else:
                if actual_sha != log_sha:
                    reasons.append("验证日志内容与记录的 hash 不一致（日志被改动）")

    fingerprint = verification.get("worktree_fingerprint")
    if fingerprint is None:
        reasons.append("执行时无法计算工作树指纹（非 git 目录），代码绑定不可验证")
    else:
        current = worktree_fingerprint(paths.root)
        if current is None:
            reasons.append("当前无法计算工作树指纹（非 git 目录），无法确认代码未变")
        elif current != fingerprint:
            reasons.append("代码已变化（工作树指纹不一致），evidence 已过期，请重新执行验证")
    return reasons


def strict_reasons(state: dict, requirement: dict, paths: ProjectPaths, latest: dict | None) -> list[str]:
    """strict 模式的额外阻塞原因；仅对「最新 evidence 声称 success」的项生效。

    手工声明（manual，即没有 execution verification 记录）的 success 在
    strict 下不再满足门禁；非 success 的项由基础规则阻塞，这里不重复。
    """
    if not state["contract"].get("strict", False):
        return []
    if latest is None or latest.get("result") not in EVIDENCE_RESULTS:
        return []
    if latest.get("result") != "success":
        return []
    verification = latest.get("verification")
    if not isinstance(verification, dict) or verification.get("mode") != "execution":
        return [
            f"strict 模式：最新 evidence {latest.get('id')} 是手工声明（manual），不是执行验证（execution）；"
            "请用 memory-corridor evidence verify 重新执行真实验证"
        ]
    return execution_stale_reasons(latest, paths)
