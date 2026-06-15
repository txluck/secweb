"""任务 CRUD 与生命周期路由 - /api/tasks 系列 + /api/concurrency + /api/config + /api/skills。"""
from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException

from .. import store
from ..deps import CFG, get_scheduler, require_auth

URL_RE = re.compile(r"^https?://", re.IGNORECASE)

router = APIRouter(dependencies=[Depends(require_auth)])


# ──── /api/skills 缓存 ────────────────────────────────────────────────
# 扫描 ~/.claude/skills/ 提取每个 SKILL.md 的 frontmatter, 30 秒缓存防止
# 频繁 IO. 没有 SKILL.md 的目录跳过 (向后兼容旧用户).
_SKILLS_CACHE: list[dict] | None = None
_SKILLS_CACHE_TS: float = 0.0
_SKILLS_CACHE_TTL = 30.0  # 秒
_SKILLS_DIR = Path.home() / ".claude" / "skills"

# 主流水线 skill 置顶顺序 (其他按字母序)
_SKILL_PIN_ORDER = ["hack", "bug-bounty", "src-hunt", "pentest"]


def _parse_skill_frontmatter(skill_md: Path) -> dict | None:
    """解析 SKILL.md 顶部 YAML frontmatter, 提取 description.

    name 字段不用 — Claude Code 实际加载 skill 用的是**目录名** (slash command
    /<dirname> 触发), frontmatter name 仅作元数据展示. 二者不一致时 (常见,
    用户从别处复制 SKILL.md 没改 frontmatter), 必须用目录名才能与 AI 实际行为对齐.

    格式约定:
      ---
      name: <skill-name>
      description: <one-line summary>
      ---

    返回 None 表示不是合法 frontmatter; 返回 {"description": ...} (可为空).
    """
    try:
        text = skill_md.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return None
    if not text.startswith("---"):
        return None
    end = text.find("\n---", 3)
    if end < 0:
        return None
    fm = text[3:end].strip()
    desc = ""
    for line in fm.splitlines():
        line = line.strip()
        if line.startswith("description:"):
            desc = line.split(":", 1)[1].strip()
            break
    return {"description": desc}


def _load_skills_list(force: bool = False) -> list[dict]:
    """扫描 ~/.claude/skills/, 返回排序后的 skill 列表 (30 秒缓存).

    skill 标识用**目录名** (与 Claude Code 加载的 slash command /<dirname> 一致).
    description 取自 SKILL.md frontmatter (展示用).

    返回元素: {"name": "<dirname>", "description": "...", "value": "/<dirname> {url}"}
    """
    global _SKILLS_CACHE, _SKILLS_CACHE_TS
    now = time.time()
    if (not force) and _SKILLS_CACHE is not None and (now - _SKILLS_CACHE_TS) < _SKILLS_CACHE_TTL:
        return _SKILLS_CACHE

    if not _SKILLS_DIR.is_dir():
        _SKILLS_CACHE = []
        _SKILLS_CACHE_TS = now
        return _SKILLS_CACHE

    skills: list[dict] = []
    try:
        for child in _SKILLS_DIR.iterdir():
            if not child.is_dir() or child.name.startswith("."):
                continue
            skill_md = child / "SKILL.md"
            if not skill_md.is_file():
                continue
            meta = _parse_skill_frontmatter(skill_md)
            if meta is None:
                continue  # 不是合法 frontmatter, 跳过
            # 关键: 用目录名作 skill 标识, 与 Claude Code 实际加载行为一致
            skills.append({
                "name": child.name,
                "description": meta["description"],
                "value": f"/{child.name} {{url}}",
            })
    except OSError:
        pass

    # 主流水线置顶 + 其他字母序
    pin_set = set(_SKILL_PIN_ORDER)
    pinned = sorted(
        (s for s in skills if s["name"] in pin_set),
        key=lambda s: _SKILL_PIN_ORDER.index(s["name"]),
    )
    others = sorted(
        (s for s in skills if s["name"] not in pin_set),
        key=lambda s: s["name"],
    )
    _SKILLS_CACHE = pinned + others
    _SKILLS_CACHE_TS = now
    return _SKILLS_CACHE


@router.get("/api/config")
async def api_config():
    return {
        "default_prompt": CFG.default_prompt,
        "concurrency": get_scheduler().concurrency,
        "task_timeout": CFG.task_timeout,
    }


@router.get("/api/skills")
async def api_list_skills(refresh: bool = False):
    """列出 ~/.claude/skills/ 下所有用户 skill, 供前端动态填充提示词预设.

    Query: ?refresh=1 强制跳过缓存重扫.
    返回: {skills: [{name, description, value}], cached_at: <unix ts>}
    """
    return {
        "skills": _load_skills_list(force=refresh),
        "cached_at": _SKILLS_CACHE_TS,
        "ttl": _SKILLS_CACHE_TTL,
    }


@router.post("/api/concurrency")
async def api_set_concurrency(payload: dict):
    n = int(payload.get("concurrency", 0))
    if n <= 0:
        raise HTTPException(400, "concurrency must be > 0")
    await get_scheduler().set_concurrency(n)
    return {"ok": True, "concurrency": get_scheduler().concurrency}


@router.post("/api/tasks")
async def api_create_tasks(payload: dict):
    raw = (payload.get("urls") or "").strip()
    prompt = (payload.get("prompt") or CFG.default_prompt).strip()
    project_id = payload.get("project_id")
    # 任务级 cookie/凭据 (本次"新增目标"提交时填的, 仅这批任务用, 不影响项目其他任务)
    # 留空时 scheduler 自动用 project.auth_payload 兜底
    auth_payload = (payload.get("auth_payload") or "").strip()
    if project_id and not store.get_project(project_id):
        raise HTTPException(400, "project not found")
    if "{url}" not in prompt:
        raise HTTPException(400, "prompt must contain {url}")
    urls = [
        line.strip() for line in re.split(r"[\s,;]+", raw) if line.strip()
    ]
    valid: list[str] = []
    invalid: list[str] = []
    for u in urls:
        if URL_RE.match(u) or u.startswith("/"):
            valid.append(u)
        else:
            invalid.append(u)
    if not valid:
        raise HTTPException(400, "no valid url")
    ids = await get_scheduler().submit_urls(
        valid, prompt, project_id=project_id, auth_payload=auth_payload,
    )
    return {"ok": True, "ids": ids, "skipped": invalid}


@router.get("/api/tasks")
async def api_list_tasks(status: Optional[str] = None, project_id: Optional[str] = None):
    return {
        "tasks": store.list_tasks(status=status, project_id=project_id),
        "stats": store.stats(project_id=project_id),
    }


@router.get("/api/tasks/{tid}")
async def api_get_task(tid: str):
    t = store.get_task(tid)
    if not t:
        raise HTTPException(404)
    return t


@router.get("/api/tasks/{tid}/events")
async def api_get_events(tid: str, after_id: int = 0, limit: int = 1000):
    if not store.get_task(tid):
        raise HTTPException(404)
    return {"events": store.get_events(tid, after_id=after_id, limit=limit)}


@router.post("/api/tasks/{tid}/stop")
async def api_stop(tid: str):
    ok = await get_scheduler().stop_task(tid)
    return {"ok": ok}


@router.post("/api/tasks/{tid}/answer")
async def api_answer(tid: str, payload: dict):
    answer = (payload.get("answer") or "").strip()
    if not answer:
        raise HTTPException(400, "empty answer")
    ok = await get_scheduler().resume_task(tid, answer)
    if not ok:
        raise HTTPException(400, "task not waiting for input")
    return {"ok": True}


@router.post("/api/tasks/{tid}/retry")
async def api_retry(tid: str, payload: dict | None = None):
    fresh = True
    if payload is not None:
        fresh = bool(payload.get("fresh", True))
    ok = await get_scheduler().retry_task(tid, fresh=fresh)
    if not ok:
        raise HTTPException(400, "task is currently running or queued")
    return {"ok": True}


@router.post("/api/tasks/{tid}/resume-rerun")
async def api_resume_rerun(tid: str):
    """沿用原 session_id 重跑, 上下文保留."""
    ok = await get_scheduler().retry_task(tid, fresh=False)
    if not ok:
        raise HTTPException(400, "task is currently running or queued")
    return {"ok": True}


@router.post("/api/tasks/{tid}/followup")
async def api_followup(tid: str, payload: dict):
    """在已完成会话上继续追问."""
    question = (payload.get("question") or "").strip()
    if not question:
        raise HTTPException(400, "empty question")
    ok = await get_scheduler().followup_task(tid, question)
    if not ok:
        raise HTTPException(400, "task is currently running or queued")
    return {"ok": True}


@router.post("/api/tasks/{tid}/continue")
async def api_continue(tid: str, payload: dict):
    """补充提示词后继续: running 会先停再续, 其他终态直接续. 沿用原 session_id."""
    prompt = (payload.get("prompt") or "").strip()
    if not prompt:
        raise HTTPException(400, "empty prompt")
    if not store.get_task(tid):
        raise HTTPException(404)
    ok = await get_scheduler().continue_with(tid, prompt)
    if not ok:
        raise HTTPException(400, "cannot continue this task")
    return {"ok": True}


@router.post("/api/tasks/{tid}/pause")
async def api_pause(tid: str):
    """SIGSTOP 暂停 claude, playwright 浏览器保持打开供手动操作."""
    if not store.get_task(tid):
        raise HTTPException(404)
    ok = await get_scheduler().pause_task(tid)
    if not ok:
        raise HTTPException(400, "task is not running")
    return {"ok": True}


@router.post("/api/tasks/{tid}/unpause")
async def api_unpause(tid: str):
    """SIGCONT 恢复被暂停的 claude 进程."""
    if not store.get_task(tid):
        raise HTTPException(404)
    ok = await get_scheduler().unpause_task(tid)
    if not ok:
        raise HTTPException(400, "task is not paused")
    return {"ok": True}


@router.post("/api/tasks/{tid}/await-login")
async def api_await_login(tid: str):
    """停止任务, 打开有头浏览器供手动登录, 状态变为 awaiting_login."""
    if not store.get_task(tid):
        raise HTTPException(404)
    ok = await get_scheduler().open_login_browser(tid)
    if not ok:
        raise HTTPException(500, "无法启动浏览器 (未找到 chromium 二进制，请确认已安装 playwright-mcp)")
    return {"ok": True}


@router.post("/api/tasks/{tid}/login-done")
async def api_login_done(tid: str, payload: dict | None = None):
    """关闭登录浏览器, 继续执行任务."""
    if not store.get_task(tid):
        raise HTTPException(404)
    prompt = ((payload or {}).get("prompt") or "").strip()
    ok = await get_scheduler().login_done(tid, prompt=prompt)
    if not ok:
        raise HTTPException(400, "task is not in awaiting_login state")
    return {"ok": True}


@router.delete("/api/tasks/{tid}")
async def api_delete(tid: str):
    t = store.get_task(tid)
    if not t:
        raise HTTPException(404)
    if t["status"] in ("running", "queued"):
        await get_scheduler().stop_task(tid)
    # 删除任务前先回收 daemon (即使是 paused / done 状态, 残留的 daemon 也要清掉)
    try:
        await get_scheduler().cleanup_task_daemon(tid)
    except Exception:
        pass
    await store.delete_task(tid)
    return {"ok": True}


@router.delete("/api/tasks")
async def api_delete_batch(project_id: Optional[str] = None):
    """批量清空项目下任务. 默认跳过活动任务 (running/queued/needs_input/paused/awaiting_login).

    用例: 项目保留 (含授权 / 默认 prompt / 并发设置), 只清旧任务后重新提交.
    """
    # 仅删可安全删除的, 活动任务保留
    ids = store.list_deletable_task_ids(project_id)
    deleted = 0
    sched = get_scheduler()
    for tid in ids:
        try:
            await sched.cleanup_task_daemon(tid)
        except Exception:
            pass
        try:
            await store.delete_task(tid)
            deleted += 1
        except Exception:
            pass
    return {"ok": True, "deleted": deleted, "total_candidates": len(ids)}


@router.get("/api/tasks/{tid}/files")
async def api_files(tid: str):
    t = store.get_task(tid)
    if not t:
        raise HTTPException(404)
    base = Path(t["workdir"])
    if not base.exists():
        return {"files": []}
    out = []
    for p in base.rglob("*"):
        if p.is_file():
            try:
                out.append({
                    "path": str(p.relative_to(base)),
                    "size": p.stat().st_size,
                })
            except OSError:
                continue
    out.sort(key=lambda x: x["path"])
    return {"files": out[:500]}
