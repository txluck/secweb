"""路由聚合 - 把各 router 装到 FastAPI app 里。"""
from . import auth, projects, reports, settings, tasks, ws

ROUTERS = (auth.router, projects.router, tasks.router, reports.router, settings.router, ws.router)


def include_all(app) -> None:
    for r in ROUTERS:
        app.include_router(r)
