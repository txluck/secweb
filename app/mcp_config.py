"""为每个任务生成独立 MCP 配置 (主要为 playwright 隔离 userDataDir).

设计取舍:
- claude CLI 在当前版本对 HTTP/SSE 远端 MCP 支持有限 (--strict-mcp-config 校验过,
  但不真正加载工具), 因此此处仍用 stdio 形式启动 playwright-mcp。
- 每个任务一个独立 user-data-dir, 避免:
  - SingletonLock 在并发任务间互锁 (同一个 profile 同时被多个 chromium 打开 → 卡死)
  - Cookie / Storage 串场, 造成越权类漏洞误报
- user-data-dir 在磁盘上持久化登录态: 即使 claude 退出 / playwright-mcp 退出,
  下次同一 task 续跑时 chromium 用同一 profile, **登录态自动延用**。

playwright-mcp 的 --isolated 与 --user-data-dir 互斥, 这里只用 --user-data-dir。
"""
from __future__ import annotations

import json
from pathlib import Path

_USER_MCP = Path.home() / ".claude" / ".mcp.json"


def _read_base() -> dict:
    if _USER_MCP.exists():
        try:
            return json.loads(_USER_MCP.read_text())
        except Exception:
            pass
    return {"mcpServers": {}}


def build_task_mcp_config(workdir: Path, *, task_id: str | None = None) -> Path:
    """生成 workdir/.mcp.json, 给 playwright server 注入任务级 --user-data-dir。

    其他 stdio mcp server (jadx 等) 原样保留。
    task_id 参数仅为向后兼容签名, 当前实现未使用 (保留以便未来切回 HTTP 路径)。
    """
    _ = task_id  # 暂未使用, 抑制 lint
    base = _read_base()
    servers = base.setdefault("mcpServers", {})
    pw = servers.get("playwright")
    if pw:
        # 复制 playwright server 配置, 清掉旧的 user-data-dir / storage-state / isolated,
        # 然后只追加任务级 --user-data-dir
        args = list(pw.get("args") or [])
        cleaned: list[str] = []
        skip = False
        for a in args:
            if skip:
                skip = False
                continue
            if a in ("--user-data-dir", "--storage-state"):
                skip = True
                continue
            if a.startswith("--user-data-dir=") or a.startswith("--storage-state="):
                continue
            if a == "--isolated":
                # 与 --user-data-dir 互斥, 直接丢弃
                continue
            cleaned.append(a)
        ud = workdir / ".pw-profile"
        ud.mkdir(parents=True, exist_ok=True)
        cleaned.append(f"--user-data-dir={ud}")
        pw = dict(pw)
        pw["args"] = cleaned
        # 显式声明 stdio (claude CLI 这个版本只稳定支持 stdio MCP)
        pw.setdefault("type", "stdio")
        servers["playwright"] = pw

    out = workdir / ".mcp.json"
    out.write_text(json.dumps(base, ensure_ascii=False, indent=2))
    return out
