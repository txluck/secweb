"""模块注册表 - 给未来的扩展模块 (资产监控/主动收集/Nuclei 联动等) 留扩展点。

每个模块声明:
- name:       唯一名
- routers:    需要挂载的 APIRouter 列表
- executors:  需要注册到 executors.REGISTRY 的执行器类列表 (可选)
- on_startup: lifespan 启动钩子 (可选, 接收 app)

启用方式:
    from app.modules.assets import AssetModule
    ENABLED_MODULES = [AssetModule()]

之后调用 register_modules(app) 即一次性挂上所有路由 + 执行器。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Protocol

from fastapi import APIRouter, Depends, FastAPI

from .deps import require_auth
from .executors import REGISTRY as EXECUTOR_REGISTRY


class Module(Protocol):
    name: str
    routers: list[APIRouter]
    executors: list[type]
    # async def on_startup(self, app: FastAPI) -> None: ...


@dataclass
class BaseModule:
    """模块基类 - 子类按需覆盖字段。"""
    name: str = ""
    routers: list[APIRouter] = field(default_factory=list)
    executors: list[type] = field(default_factory=list)
    on_startup: Callable[[FastAPI], Any] | None = None


# 全部启用的模块 - 新增模块时在此追加实例
ENABLED_MODULES: list[Module] = []


_meta_router = APIRouter(prefix="/api", tags=["meta"])


@_meta_router.get("/_modules", dependencies=[Depends(require_auth)])
async def _list_modules():
    return {
        "modules": [
            {
                "name": getattr(m, "name", ""),
                "executors": [
                    getattr(e, "type", "") for e in getattr(m, "executors", []) or []
                ],
                "routes": sum(
                    len(getattr(r, "routes", [])) for r in getattr(m, "routers", []) or []
                ),
            }
            for m in ENABLED_MODULES
        ],
        "executors": list(EXECUTOR_REGISTRY.keys()),
    }


def register_modules(app: FastAPI) -> None:
    """把 ENABLED_MODULES 中所有模块挂载到 app:
    - 注册路由
    - 注册执行器 (写入 executors.REGISTRY)
    - 暴露 /api/_modules 列出已启用模块
    """
    for mod in ENABLED_MODULES:
        for r in getattr(mod, "routers", []) or []:
            app.include_router(r)
        for ex in getattr(mod, "executors", []) or []:
            EXECUTOR_REGISTRY[ex.type] = ex

    app.include_router(_meta_router)
