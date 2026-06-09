"""项目 CRUD 路由 - /api/projects 系列。"""
from __future__ import annotations

import shutil

from fastapi import APIRouter, Depends, HTTPException

from .. import store
from ..deps import CFG, get_scheduler, require_auth

router = APIRouter(prefix="/api/projects", dependencies=[Depends(require_auth)])


@router.get("")
async def api_list_projects():
    return {"projects": store.list_projects()}


@router.post("")
async def api_create_project(payload: dict):
    name = (payload.get("name") or "").strip()
    if not name:
        raise HTTPException(400, "name required")
    desc = (payload.get("description") or "").strip()
    dprompt = (payload.get("default_prompt") or "").strip()
    conc = int(payload.get("concurrency") or 3)
    auth_payload = (payload.get("auth_payload") or payload.get("cookies") or "").strip()
    pid = await store.create_project(name, desc, dprompt, conc, auth_payload)
    return {"ok": True, "id": pid}


@router.get("/{pid}")
async def api_get_project(pid: str):
    p = store.get_project(pid)
    if not p:
        raise HTTPException(404)
    p["stats"] = store.stats(project_id=pid)
    p["concurrency_effective"] = get_scheduler().project_concurrency(pid)
    return p


@router.patch("/{pid}")
async def api_update_project(pid: str, payload: dict):
    if not store.get_project(pid):
        raise HTTPException(404)
    fields = {}
    for k in ("name", "description", "default_prompt", "auth_payload"):
        if k in payload:
            fields[k] = (payload[k] or "").strip()
    if "concurrency" in payload:
        n = max(1, min(int(payload["concurrency"]), 32))
        fields["concurrency"] = n
    if not fields:
        return {"ok": True}
    await store.update_project(pid, **fields)
    # 如果改了并发, 同步刷 scheduler
    if "concurrency" in fields:
        await get_scheduler().set_project_concurrency(pid, fields["concurrency"])
    return {"ok": True}


@router.post("/{pid}/concurrency")
async def api_set_project_concurrency(pid: str, payload: dict):
    """单独设置项目并发, 立即生效, 不影响其他项目。"""
    if not store.get_project(pid):
        raise HTTPException(404)
    n = int(payload.get("concurrency", 0))
    if n <= 0:
        raise HTTPException(400, "concurrency must be > 0")
    ok = await get_scheduler().set_project_concurrency(pid, n)
    if not ok:
        raise HTTPException(404)
    return {"ok": True, "concurrency": get_scheduler().project_concurrency(pid)}


@router.delete("/{pid}")
async def api_delete_project(pid: str, cascade: bool = False):
    if not store.get_project(pid):
        raise HTTPException(404)
    if cascade:
        for t in store.list_tasks(project_id=pid):
            if t["status"] in ("running", "queued", "needs_input"):
                await get_scheduler().stop_task(t["id"])
    n = await store.delete_project(pid, cascade=cascade)
    if n == -1:
        raise HTTPException(400, "项目下还有任务，使用 ?cascade=true 强制删除")
    if cascade:
        proj_dir = CFG.runs_dir / pid
        if proj_dir.exists():
            shutil.rmtree(proj_dir, ignore_errors=True)
    return {"ok": True, "deleted_tasks": n}
