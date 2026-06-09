"""启动前自检: 校验 playwright MCP 在当前 claude 配置下可达."""
from __future__ import annotations

import asyncio
import sys


async def warn_if_missing(claude_bin: str) -> None:
    """跑一次 `claude mcp list`, 若没看到 playwright 就警告."""
    try:
        proc = await asyncio.create_subprocess_exec(
            claude_bin, "mcp", "list",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out, err = await asyncio.wait_for(proc.communicate(), timeout=15)
    except (asyncio.TimeoutError, FileNotFoundError, Exception):
        return
    text = (out + err).decode(errors="replace").lower()
    if "playwright" not in text:
        print(
            "⚠️  Playwright MCP 未检测到。Web 测试会降级到 Python,"
            "影响 DOM/网络面板/JS evaluate。请检查:\n"
            "   - `~/.claude/.mcp.json` 是否含 playwright server\n"
            "   - `~/.claude/settings.json` 中 enabledMcpjsonServers 是否包含 'playwright'",
            file=sys.stderr,
        )
