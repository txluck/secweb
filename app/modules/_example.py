"""模块示例 / 模板 - 复制此文件作为新模块起点。

新模块开发指南:
1. 复制本文件到 app/modules/<your_name>.py 或目录
2. 实现 router / executors / migrations / startup hooks (按需)
3. 在 web.py 的 ENABLED_MODULES 列表里加 from .modules.<name> import get_module
4. 重启服务即可

资产监控模块的设想结构 (假想):
    name: "assets"
    routers: 资产 CRUD + 子域查询接口
    executors: SubfinderExecutor / HttpxExecutor / NaabuExecutor
    migrations: 创建 assets / asset_watchers / scan_records 表
    on_startup: 启动周期性扫描调度
"""
from __future__ import annotations

from fastapi import APIRouter, Depends

from ..executors.base import ExecutorContext, ExecutorResult, TaskExecutor
from ..registry import Module
from ..routers.deps import require_auth

# ----- 路由示例 -----
_router = APIRouter(prefix="/api/example", dependencies=[Depends(require_auth)])


@_router.get("/ping")
async def ping():
    return {"ok": True, "module": "_example"}


# ----- 执行器示例 (不会真的跑, 仅展示形状) -----
class _NoopExecutor:
    type: str = "_example_noop"

    async def run(self, task: dict, ctx: ExecutorContext) -> ExecutorResult:
        return ExecutorResult(status="done", exit_code=0)


# ----- 数据迁移钩子示例 -----
def _migrate(conn) -> None:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS _example_records "
        "(id INTEGER PRIMARY KEY AUTOINCREMENT, note TEXT)"
    )


def get_module() -> Module:
    return Module(
        name="_example",
        description="示例模块, 演示如何接入路由/执行器/迁移",
        routers=[_router],
        executors=[_NoopExecutor],
        migrations=[_migrate],
    )
