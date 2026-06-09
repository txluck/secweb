"""项目入口 - python -m app  或  python main.py"""
from __future__ import annotations

import asyncio

import uvicorn

from app.config import Config
from app.preflight import warn_if_missing


def main() -> None:
    cfg = Config.load()
    # 启动前自检: playwright MCP 可达性
    try:
        asyncio.run(warn_if_missing(cfg.claude_bin))
    except Exception:
        pass
    uvicorn.run(
        "app.web:app",
        host=cfg.host,
        port=cfg.port,
        log_level="info",
        reload=False,
    )


if __name__ == "__main__":
    main()
