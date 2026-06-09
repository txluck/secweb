"""报告聚合与单任务报告路由。"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException

from .. import store
from ..deps import require_auth

router = APIRouter(dependencies=[Depends(require_auth)])


@router.get("/api/tasks/{tid}/report")
async def api_report(tid: str):
    t = store.get_task(tid)
    if not t:
        raise HTTPException(404)
    rp = t.get("report_path")
    if not rp:
        return {"content": "", "missing": True}
    full = Path(t["workdir"]) / rp
    if not full.exists():
        return {"content": "", "missing": True}
    try:
        return {"content": full.read_text(encoding="utf-8", errors="replace")}
    except OSError as e:
        raise HTTPException(500, str(e))


@router.get("/api/reports")
async def api_reports_global(project_id: Optional[str] = None, limit: int = 200):
    items = store.list_reports(project_id=project_id, limit=limit)
    out = []
    for r in items:
        full = Path(r["workdir"]) / r["report_path"]
        size = 0
        preview = ""
        if full.exists():
            try:
                size = full.stat().st_size
                preview = full.read_text(encoding="utf-8", errors="replace")[:400]
            except OSError:
                pass
        out.append({**r, "size": size, "preview": preview})
    return {"reports": out}


@router.get("/api/projects/{pid}/reports")
async def api_project_reports(pid: str, limit: int = 200):
    if not store.get_project(pid):
        raise HTTPException(404)
    return await api_reports_global(project_id=pid, limit=limit)
