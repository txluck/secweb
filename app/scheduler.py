"""任务调度器 - asyncio.Semaphore 控并发, 单进程内常驻。

调度器不再直接调 runner, 而是通过 executors 注册表分发到具体执行器。
新增任务类型 = 在 executors 里加一个类 + REGISTRY 注册一行。
"""
from __future__ import annotations

import asyncio
import glob
import os
import shutil
import signal
import subprocess
import time
from pathlib import Path
from typing import Awaitable, Callable
from urllib.parse import urlparse

from . import runner_sdk, store
from .executors import ExecutorContext, get_executor
from .system_prompt import build_task_claude_md

EventCB = Callable[[str, str, str | dict], Awaitable[None]]


def _hostname_of(u: str) -> str | None:
    """从 URL 提取 hostname, 失败返回 None.

    支持 http(s):// 前缀, 也支持裸 host:port. 去掉端口段, 仅保留 host.
    """
    if not u:
        return None
    try:
        if "://" not in u:
            u = "http://" + u
        host = urlparse(u).hostname
        return host.lower() if host else None
    except Exception:
        return None


def _build_in_scope_assets_block(
    current_url: str,
    project_id: str | None,
) -> str:
    """从当前 URL + 同 project 历史任务 URL 收集 hostname 集合,
    渲染成授权书占位符 {IN_SCOPE_ASSETS} 的资产清单 markdown.

    设计原则:
      - 仅列出曾经被 dashboard 接受过的 hostname (历史任务) + 当前任务的 hostname
      - 不做 tld 归并 (不引入 publicsuffix 依赖, 不猜测域族归属)
      - 同一 hostname 一行, 去重并排序, 输出形如 "### 当前任务\\n- `<hostname>`"
      - 历史任务的 hostname 单独成段, 表示"本 dashboard 长期负责的资产范围"
      - project_id=None (无项目) 时仅列当前 URL, 不展开全 dashboard 历史,
        避免向 AI 暴露与本任务无关的资产(那会让 scope 自相矛盾,
        AI 反而怀疑授权书合法性).
    """
    cur_host = _hostname_of(current_url)
    history_hosts: set[str] = set()
    # 关键: project_id=None 时不查历史 (store.list_tasks(None) 会返回所有项目)
    if project_id:
        try:
            for t in store.list_tasks(project_id=project_id, limit=500):
                h = _hostname_of(t.get("url") or "")
                if h:
                    history_hosts.add(h)
        except Exception:
            pass

    # 当前 URL 总是单独高亮一行, 让 AI 一眼看到本次目标在 scope 内
    lines: list[str] = []
    if cur_host:
        lines.append("### 当前任务目标")
        lines.append(f"- `{cur_host}` (本次 /hack 指令的目标)")
        history_hosts.discard(cur_host)

    if history_hosts:
        lines.append("")
        lines.append("### 同项目历史已授权目标")
        for h in sorted(history_hosts):
            lines.append(f"- `{h}`")

    if not lines:
        # 兜底: 无任何 host 可解析 (理论不应触发)
        lines = ["### 当前任务目标", "- (无法解析 hostname, 请人工确认目标 URL)"]

    return "\n".join(lines)


def _is_miniprogram_target(url: str) -> bool:
    """检测目标是否为小程序反编译目录.

    判定: 本地路径 (以 / 开头) + 目录存在 + 含小程序特征文件.
    通用 — 不依赖具体 AppID 或品牌, 仅按结构特征 (app.json / *.wxml / *.wxss
    / *.wxapkg).

    True 时 scheduler 切换到 _CLAUDE_MD_TOOL_POLICY_MINIPROGRAM,
    跳过浏览器门控 + 降级禁令 (这些对反编译目录场景无意义).

    性能保护: 最多扫 _MP_SCAN_LIMIT 个文件就返回, 避免大目录 (含
    node_modules / 第三方库等) 卡住 scheduler 提交.
    """
    if not url or not url.startswith("/"):
        return False
    p = Path(url)
    try:
        if not p.is_dir():
            return False
    except OSError:
        return False
    # 顶层 app.json 是最快路径
    try:
        if (p / "app.json").is_file():
            return True
    except OSError:
        return False
    # 否则浅扫前 3 层, 文件数硬上限防大目录卡顿
    _MP_SCAN_LIMIT = 500
    scanned = 0
    try:
        for child in p.iterdir():
            scanned += 1
            if scanned > _MP_SCAN_LIMIT:
                return False
            if child.is_file() and child.suffix in (".wxml", ".wxss", ".wxapkg"):
                return True
            if child.is_dir():
                try:
                    for grand in child.iterdir():
                        scanned += 1
                        if scanned > _MP_SCAN_LIMIT:
                            return False
                        if grand.is_file() and grand.suffix in (".wxml", ".wxss"):
                            return True
                        if grand.is_dir():
                            try:
                                for ggrand in grand.iterdir():
                                    scanned += 1
                                    if scanned > _MP_SCAN_LIMIT:
                                        return False
                                    if (ggrand.is_file() and
                                            ggrand.suffix in (".wxml", ".wxss")):
                                        return True
                            except (OSError, PermissionError):
                                continue
                except (OSError, PermissionError):
                    continue
    except (OSError, PermissionError):
        return False
    return False

class Scheduler:
    """每个项目独立维护一个 Semaphore, 项目间并发互不影响。
    无项目的任务 (project_id=None) 走 _global semaphore 兜底, 默认值=default_concurrency。
    """

    DEFAULT_PID = "_"  # 无项目任务的 sentinel key

    def __init__(
        self,
        *,
        claude_bin: str,
        runs_dir: Path,
        timeout: int,
        concurrency: int,
        on_event: EventCB | None = None,
    ) -> None:
        self.claude_bin = claude_bin
        self.runs_dir = runs_dir
        self.timeout = timeout
        self._default_concurrency = concurrency
        self._on_event = on_event
        # 每项目一把 semaphore + 配额记录
        self._sems: dict[str, asyncio.Semaphore] = {}
        self._sem_caps: dict[str, int] = {}
        # 跟踪运行中的协程, 用于 stop / shutdown
        self._tasks: dict[str, asyncio.Task] = {}
        self._lock = asyncio.Lock()

    @property
    def concurrency(self) -> int:
        """全局默认并发 (用于 UI 兜底显示)。"""
        return self._default_concurrency

    def project_concurrency(self, project_id: str | None) -> int:
        """读取某项目当前生效并发。"""
        key = project_id or self.DEFAULT_PID
        if key in self._sem_caps:
            return self._sem_caps[key]
        if project_id:
            p = store.get_project(project_id)
            if p and p.get("concurrency"):
                return int(p["concurrency"])
        return self._default_concurrency

    def _sem_for(self, project_id: str | None) -> asyncio.Semaphore:
        """取项目 semaphore, 不存在则按 projects.concurrency 创建。"""
        key = project_id or self.DEFAULT_PID
        sem = self._sems.get(key)
        if sem is not None:
            return sem
        cap = self.project_concurrency(project_id)
        sem = asyncio.Semaphore(cap)
        self._sems[key] = sem
        self._sem_caps[key] = cap
        return sem

    async def set_default_concurrency(self, n: int) -> None:
        """改全局默认并发 (只影响新出现的、还没建过 semaphore 的项目)。"""
        n = max(1, min(n, 32))
        self._default_concurrency = n

    async def set_project_concurrency(self, project_id: str, n: int) -> bool:
        """改某项目并发上限。已运行的任务持有旧 semaphore 不受影响, 新任务用新值。"""
        n = max(1, min(n, 32))
        if not store.get_project(project_id):
            return False
        await store.update_project(project_id, concurrency=n)
        # 替换该项目的 semaphore
        self._sems[project_id] = asyncio.Semaphore(n)
        self._sem_caps[project_id] = n
        return True

    # 兼容旧接口 (POST /api/concurrency 仍走全局默认)
    async def set_concurrency(self, n: int) -> None:
        await self.set_default_concurrency(n)

    async def submit_urls(
        self, urls: list[str], prompt_template: str, project_id: str | None = None,
    ) -> list[str]:
        ids: list[str] = []
        # 项目级认证数据注入: 提交时一次性读取, 避免每个任务都查
        auth_payload = ""
        if project_id:
            proj = store.get_project(project_id)
            if proj:
                auth_payload = (proj.get("auth_payload") or proj.get("cookies") or "").strip()
        for url in urls:
            url = url.strip()
            if not url:
                continue
            prompt = (
                prompt_template
                .replace("{url}", url)
                .replace("{auth}", auth_payload)
                .replace("{cookies}", auth_payload)
            )
            tid = await store.create_task(
                url=url,
                prompt=prompt,
                workdir="",  # 占位, 下面更新
                project_id=project_id,
            )
            # 项目下的任务跑在 runs/<project_id>/<task_id>/ ; 无项目则放 runs/_/<task_id>/
            sub = project_id or "_"
            workdir = self.runs_dir / sub / tid
            workdir.mkdir(parents=True, exist_ok=True)
            # 注入任务级 CLAUDE.md: 项目元信息 + 授权范围 + 工具约束
            # claude 启动时会自发现 CWD 下的 CLAUDE.md, 解决并发任务看不到授权书问题
            # 模板与文案统一来自 app/system_prompt.py (单一来源, 避免与 SYSTEM_APPEND 漂移)
            try:
                project_root = Path(__file__).resolve().parent.parent
                authz = project_root / "授权书.md"
                authz_text = (
                    authz.read_text(encoding="utf-8") if authz.exists() else None
                )
                # 动态替换 {IN_SCOPE_ASSETS} 占位符:
                # 把当前 URL + 同 project 历史 URL 的 hostname 集合渲染成资产清单,
                # 避免授权书硬编码特定域族 (历史 bug: 5 份失败报告对应的目标必须
                # 在授权书清单里 AI 才不拒绝, 但硬编码 = 新域族无效).
                if authz_text and "{IN_SCOPE_ASSETS}" in authz_text:
                    assets_block = _build_in_scope_assets_block(url, project_id)
                    authz_text = authz_text.replace(
                        "{IN_SCOPE_ASSETS}", assets_block
                    )
                # 小程序模式: 本地反编译目录 → 切到不含浏览器规则的精简版 rule.
                # 浏览器门控 / 降级禁令对反编译目录场景无意义, 反而误导 AI.
                is_miniprogram = _is_miniprogram_target(url)
                claude_md_text = build_task_claude_md(
                    proj, url, authz_text,
                    is_miniprogram=is_miniprogram,
                )
                (workdir / "CLAUDE.md").write_text(claude_md_text, encoding="utf-8")
            except Exception:
                pass
            await store.update_task(tid, workdir=str(workdir))
            ids.append(tid)
            self._spawn(tid, resume=False, extra_input=None)
        return ids

    async def resume_task(self, tid: str, user_answer: str) -> bool:
        t = store.get_task(tid)
        if not t or t["status"] != "needs_input":
            return False
        await store.update_task(tid, status="queued", pending_question=None)
        self._spawn(tid, resume=True, extra_input=user_answer)
        return True

    async def followup_task(self, tid: str, question: str) -> bool:
        """在已完成/失败/停止的任务上,沿用原 session_id 续问新问题.
        走 --resume <session_id> -p <question>, 上下文完整保留."""
        t = store.get_task(tid)
        if not t or t["status"] in ("running", "queued"):
            return False
        await store.update_task(
            tid, status="queued", pending_question=None,
            exit_code=None, finished_at=None,
        )
        self._spawn(tid, resume=True, extra_input=question)
        return True

    async def continue_with(self, tid: str, prompt: str) -> bool:
        """补充提示词后继续:

        - running / paused: 直接在活跃 SDK client 上 query(prompt), 不重启;
          paused 状态会同步 unpause 让 pump_messages 解除阻塞。
        - 其他终态 (done/failed/stopped/needs_input): SDK client 已断开,
          走 --resume <session_id> 路径重新拉起 (同 session_id, 上下文连续)。
        """
        t = store.get_task(tid)
        if not t:
            return False
        prompt = (prompt or "").strip()
        if not prompt:
            return False
        status = t["status"]
        if status in ("running", "paused"):
            # 若处于 paused, 先 unpause 让 pump_messages 继续吃后续消息
            if status == "paused":
                runner_sdk.set_paused(tid, False)
                await store.update_task(tid, status="running")
                if self._on_event:
                    await self._on_event(tid, "status", "running")
            # SDK client 上注入新 user message
            ok = await runner_sdk.inject_followup(tid, prompt)
            if ok:
                return True
            # 注入失败: client 死了, 兜底走重起路径
        # 其他终态 / 注入失败 → 同 session_id 重新拉起 SDK
        await store.update_task(
            tid, status="queued", pending_question=None,
            exit_code=None, finished_at=None,
        )
        self._spawn(tid, resume=True, extra_input=prompt)
        return True

    async def retry_task(self, tid: str, fresh: bool = True) -> bool:
        """重跑任务. fresh=True (默认) 用新 session_id 全新开始;
        fresh=False 沿用原 session_id (适合从 needs_input 之外的失败处续上下文)."""
        t = store.get_task(tid)
        if not t:
            return False
        if t["status"] in ("running", "queued"):
            return False
        fields: dict = dict(
            status="queued", pending_question=None,
            exit_code=None, finished_at=None,
        )
        if fresh:
            import uuid as _uuid
            fields["session_id"] = str(_uuid.uuid4())
        await store.update_task(tid, **fields)
        self._spawn(tid, resume=False, extra_input=None)
        return True

    async def stop_task(self, tid: str) -> bool:
        t = store.get_task(tid)
        if not t:
            return False
        # 让活跃 SDK client 断开 (中断当前 turn + 释放 mcp 子进程)
        try:
            await runner_sdk.stop_task(tid)
        except Exception:
            pass
        # 取消 wrapper 协程, 并等它真正退出 (释放 semaphore + 触发 done_callback)
        async with self._lock:
            task = self._tasks.get(tid)
        if task and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        # 状态在 _wrap finally 兜底, 这里强制写一遍
        await store.update_task(
            tid, status="stopped", finished_at=time.time(), pid=None
        )
        if self._on_event:
            await self._on_event(tid, "status", "stopped")
        return True

    def _spawn(self, tid: str, *, resume: bool, extra_input: str | None) -> None:
        coro = self._wrap(tid, resume=resume, extra_input=extra_input)
        task = asyncio.create_task(coro, name=f"task-{tid}")
        self._tasks[tid] = task
        # 仅当 _tasks[tid] 仍指向自己时才 pop, 避免覆盖后续 spawn 的新任务
        def _pop(_t: asyncio.Task, _id: str = tid, _self: asyncio.Task = task) -> None:
            if self._tasks.get(_id) is _self:
                self._tasks.pop(_id, None)
        task.add_done_callback(_pop)

    async def _wrap(
        self, tid: str, *, resume: bool, extra_input: str | None
    ) -> None:
        t0 = store.get_task(tid) or {}
        sem = self._sem_for(t0.get("project_id"))
        async with sem:
            try:
                t = store.get_task(tid) or {"id": tid, "executor_type": "claude_sdk"}
                # v2.0: 即使 DB 里残留 executor_type='claude' (老 CLI 任务), get_executor
                # 也会回落到 ClaudeSDKExecutor (REGISTRY 中显式映射, 见 executors/__init__.py)
                exec_cls = get_executor(t.get("executor_type") or "claude_sdk")
                ctx = ExecutorContext(
                    claude_bin=self.claude_bin,
                    runs_dir=self.runs_dir,
                    timeout=self.timeout,
                    on_event=self._on_event,
                    extra={"resume": resume, "extra_input": extra_input},
                )
                await exec_cls().run(t, ctx)
            except asyncio.CancelledError:
                # stop_task 已经写过状态
                raise
            except Exception as e:
                await store.add_event(
                    tid, "stderr", f"[scheduler] executor error: {e!r}"
                )
                await store.update_task(
                    tid, status="failed", finished_at=time.time(), pid=None
                )
                if self._on_event:
                    await self._on_event(tid, "status", "failed")

    async def shutdown(self) -> None:
        # 关 watchdog
        wd = getattr(self, "_watchdog_task", None)
        if wd and not wd.done():
            wd.cancel()
            try:
                await wd
            except (asyncio.CancelledError, Exception):
                pass
        for t in list(self._tasks.values()):
            t.cancel()
        for t in list(self._tasks.values()):
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass

    # ── watchdog: 扫描 status=running 但 pid 已死的任务, 兜底改为 stopped ──
    # 处理场景:
    #   - 进程被外部 kill -9 / OOM, runner 协程没机会写终态
    #   - 系统 sleep 把 runner 协程冻住, 但 OS 上 claude 进程已经因为 API 断连退出
    #   - claude 内部异常崩溃, runner 还在等 stdout
    # 每 30s 扫一次, 把这些"僵尸 running"改成 stopped 并广播 status, 前端立刻看到。

    async def _watchdog_loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(30)
                await self._reconcile_dead_running()
            except asyncio.CancelledError:
                raise
            except Exception:
                # 不让 watchdog 自己挂掉, 兜底吃异常
                pass

    def start_watchdog(self) -> None:
        """在 lifespan 里调一次, 把 watchdog 跑成后台 task。"""
        if getattr(self, "_watchdog_task", None) and not self._watchdog_task.done():
            return
        self._watchdog_task = asyncio.create_task(
            self._watchdog_loop(), name="scheduler-watchdog"
        )

    async def _reconcile_dead_running(self) -> None:
        """SDK 版判定: 没有独立子进程 pid 可监控, 改看 wrapper 协程是否仍在跑.

        - 协程仍活: 正常, 不动
        - 协程已退但 DB 仍 running: 异常 (进程崩溃 / 异常路径漏写终态), 兜底改为 stopped
        """
        running = [t for t in store.list_tasks(status="running")]
        for t in running:
            tid = t["id"]
            wrapper = self._tasks.get(tid)
            if wrapper and not wrapper.done():
                continue  # 协程仍在跑, 正常
            # 协程不在 (或已完成) 但 DB 仍 running → 兜底
            await store.update_task(
                tid, status="stopped", finished_at=time.time(), pid=None
            )
            await store.add_event(tid, "system", {
                "event": "watchdog_reconcile",
                "reason": "wrapper_coroutine_gone",
            })
            if self._on_event:
                await self._on_event(tid, "status", "stopped")

    # ── 暂停 / 恢复 ───────────────────────────────────────────
    # SDK 化后底层不再 SIGSTOP 进程 (SDK 在主进程内, SIGSTOP 会冻整个 fastapi).
    # 改为软暂停: pump_messages 检测到 state.paused=True 就 await pause_event,
    # 真正不再消费 SDK 消息. playwright-mcp 子进程仍活, 浏览器窗口在,
    # 用户照常手动登录. unpause 时 pause_event.set() 让 pump 继续吃后续消息.

    async def pause_task(self, tid: str) -> bool:
        """暂停: 调 client.interrupt() 让模型停下, playwright-mcp 子进程仍活,
        用户可以在浏览器窗口里手动登录. 当前 turn 会以 stop_reason='interrupted'
        结束, run_task 主循环在 turn 边界 await pause_event 等 unpause."""
        t = store.get_task(tid)
        if not t or t["status"] != "running":
            return False
        ok = await runner_sdk.set_paused(tid, True)
        if not ok:
            return False
        await store.update_task(tid, status="paused")
        if self._on_event:
            await self._on_event(tid, "status", "paused")
        return True

    async def unpause_task(self, tid: str) -> bool:
        """恢复: 清 pause flag, run_task 主循环醒来用 query('继续') 重启新 turn."""
        t = store.get_task(tid)
        if not t or t["status"] != "paused":
            return False
        ok = await runner_sdk.set_paused(tid, False)
        if not ok:
            return False
        await store.update_task(tid, status="running")
        if self._on_event:
            await self._on_event(tid, "status", "running")
        return True

    # ── 兼容旧路由 (现在和 pause/unpause 等价, 保留给已部署前端) ────────

    async def open_login_browser(self, tid: str) -> bool:
        """旧"打开手动登录浏览器"接口 — 现在和 pause_task 等价。

        前端按这个按钮 = 暂停任务, 用户登录浏览器中已经打开的窗口即可。
        """
        return await self.pause_task(tid)

    async def login_done(self, tid: str, prompt: str = "") -> bool:
        """旧"已登录, 继续"接口 — 现在和 unpause_task 等价。"""
        t = store.get_task(tid)
        if not t:
            return False
        if t["status"] not in ("paused", "awaiting_login"):
            return False
        if t["status"] == "awaiting_login":
            # 历史残留状态: 关闭外置 chromium 后落到 paused, 再走标准恢复
            lpid = t.get("login_pid")
            if lpid:
                try:
                    os.kill(lpid, signal.SIGTERM)
                except (ProcessLookupError, PermissionError):
                    pass
            await store.update_task(tid, status="paused", login_pid=None)
        return await self.unpause_task(tid)

    # ── 占位: HTTP daemon 路径暂时未启用, 但保留接口防止旧调用栈炸 ──────

    async def cleanup_task_daemon(self, tid: str) -> None:
        """no-op (当前用 stdio MCP, 没有独立 daemon 需要回收)。"""
        return

    def cleanup_task_daemon_sync(self, tid: str) -> None:
        return

    def startup_cleanup(self) -> None:
        """no-op (当前架构无独立 daemon)。"""
        return


def _find_chromium() -> str | None:
    """旧"独立 chromium 登录"路径已废弃, 这函数仍保留是为了向后兼容 import。"""
    home = Path.home()
    patterns = [
        # macOS
        str(home / "Library/Caches/ms-playwright/chromium-*/chrome-mac/Chromium.app/Contents/MacOS/Chromium"),
        # Linux
        str(home / ".cache/ms-playwright/chromium-*/chrome-linux/chrome"),
    ]
    for pat in patterns:
        hits = sorted(glob.glob(pat), reverse=True)  # 最新版本优先
        if hits:
            return hits[0]
    # 降级: 系统自带 chromium / chrome
    for name in ("chromium", "chromium-browser", "google-chrome", "google-chrome-stable"):
        p = shutil.which(name)
        if p:
            return p
    return None
