"""Per-task playwright-mcp HTTP daemon.

把 playwright-mcp 从 claude 子进程 (stdio MCP) 解耦为一个**长生命周期的本地 HTTP 服务**:

    每个 task → 自己的 playwright-mcp 进程 → 监听 127.0.0.1:<随机端口>/mcp
                                          → user-data-dir = runs/<tid>/.pw-profile
    claude 通过 --mcp-config 指向这个 url, 不再 spawn 自己的 stdio mcp 子进程

收益:
- claude 进程被停掉 / 重启 / pause→resume, 浏览器全程不死 (playwright-mcp 不是 claude 的子进程)
- 用户可以手动登录浏览器, 登录态写入磁盘 profile, claude 复活后无缝继续
- 同一 task 多次 --resume 都连同一个 daemon, 复用同一浏览器上下文

daemon 的进程组与 claude 解耦 (start_new_session=True),
仅由 Scheduler 在任务彻底终止 (done/failed/stopped/deleted) 时显式回收。
"""
from __future__ import annotations

import os
import shutil
import signal
import socket
import subprocess
import time
from pathlib import Path
from typing import Tuple


def _alloc_free_port() -> int:
    """让 OS 分一个空闲 TCP 端口给我们用。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _is_alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False


def _port_open(port: int, timeout: float = 0.2) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout):
            return True
    except OSError:
        return False


def _find_pw_mcp_bin() -> str | None:
    """优先用户全局 npm bin, 退化到 PATH。"""
    cand = Path.home() / ".npm-global/bin/playwright-mcp"
    if cand.exists():
        return str(cand)
    return shutil.which("playwright-mcp")


def ensure_daemon(
    workdir: Path,
    *,
    existing_pid: int | None = None,
    existing_port: int | None = None,
    headless: bool = False,
    wait_ready_secs: float = 8.0,
) -> Tuple[int, int]:
    """保证 workdir 对应的 playwright-mcp HTTP daemon 在跑, 返回 (pid, port)。

    - 若 existing_pid 进程还活, 且 existing_port 还在监听 → 直接复用
    - 否则启一个新的, 端口由 OS 分配, 写入 workdir/.mcp-daemon (供调试)
    """
    # 复用还活着的 daemon
    if _is_alive(existing_pid) and existing_port and _port_open(existing_port):
        return existing_pid, existing_port

    bin_path = _find_pw_mcp_bin()
    if not bin_path:
        raise RuntimeError("playwright-mcp 二进制未找到 (检查 ~/.npm-global/bin 或 PATH)")

    profile = workdir / ".pw-profile"
    profile.mkdir(parents=True, exist_ok=True)

    port = _alloc_free_port()
    args = [
        bin_path,
        "--port", str(port),
        "--host", "127.0.0.1",
        f"--user-data-dir={profile}",
    ]
    if headless:
        args.append("--headless")

    log_path = workdir / ".mcp-daemon.log"
    log_fp = open(log_path, "ab", buffering=0)
    proc = subprocess.Popen(
        args,
        stdout=log_fp,
        stderr=subprocess.STDOUT,
        start_new_session=True,  # 与 claude 解耦, claude 死它不死
        cwd=str(workdir),
    )

    # 等端口起来
    deadline = time.time() + wait_ready_secs
    while time.time() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(
                f"playwright-mcp 启动失败 (rc={proc.returncode}), 详见 {log_path}"
            )
        if _port_open(port):
            break
        time.sleep(0.1)
    else:
        # 起不来, 杀掉避免遗留
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass
        raise RuntimeError(f"playwright-mcp {port} 端口在 {wait_ready_secs}s 内未就绪")

    (workdir / ".mcp-daemon").write_text(f"{proc.pid}\n{port}\n", encoding="utf-8")
    return proc.pid, port


def kill_daemon(pid: int | None) -> None:
    """彻底回收 daemon 整个进程组 (含 chromium / 各 worker)。

    用 SIGTERM 给整组发停止信号, 给一点 graceful 时间, 还活就 SIGKILL。
    最后 best-effort waitpid 回收 zombie (如果我们恰好是父进程)。
    """
    if not pid:
        return
    try:
        pgid = os.getpgid(pid)
    except (ProcessLookupError, PermissionError):
        return
    try:
        os.killpg(pgid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        return
    # 给一点时间 graceful, 再 SIGKILL
    for _ in range(15):  # ~1.5s
        if not _is_alive(pid):
            break
        time.sleep(0.1)
    if _is_alive(pid):
        try:
            os.killpg(pgid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
    # 回收 zombie (best-effort, 仅当我们是父进程时有效)
    try:
        os.waitpid(pid, os.WNOHANG)
    except (ChildProcessError, OSError):
        pass
