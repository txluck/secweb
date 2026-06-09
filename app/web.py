"""FastAPI 应用 - 装配 lifespan / 静态文件 / routers / module 注册表。

具体路由实现都在 app/routers/*.py, 数据模型在 app/models.py,
执行器在 app/executors/*.py, 共享依赖在 app/deps.py。
新增模块: 见 app/registry.py.
"""
from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from . import store
from .deps import BROADCAST, CFG, STATIC_DIR, set_scheduler
from .registry import register_modules
from .routers import include_all
from .scheduler import Scheduler


@asynccontextmanager
async def lifespan(app: FastAPI):
    store.init_db()
    CFG.runs_dir.mkdir(parents=True, exist_ok=True)
    # 把 .env 里的 ANTHROPIC_* 配置回写到 os.environ (setdefault, 不覆盖父进程已设值),
    # 之后 claude-agent-sdk 子进程会继承到这些 env, 走中转 / 自建网关
    CFG.export_anthropic_env()
    sched = Scheduler(
        claude_bin=CFG.claude_bin,
        runs_dir=CFG.runs_dir,
        timeout=CFG.task_timeout,
        concurrency=CFG.default_concurrency,
        on_event=BROADCAST.event_cb,
    )
    # 上次进程死前可能留下 mcp daemon 孤儿, 先清一遍
    sched.startup_cleanup()
    set_scheduler(sched)
    # watchdog: 30s 扫一次, 把 status=running 但进程已死的任务兜底成 stopped
    sched.start_watchdog()
    yield
    await sched.shutdown()


app = FastAPI(title="secweb", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
register_modules(app)
include_all(app)
