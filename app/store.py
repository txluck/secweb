"""SQLite 持久化层。任务、事件、用户输入。

设计说明:
- tasks: 一行 = 一个 URL 的渗透任务
- events: 任务执行过程中产生的事件流(stdout/stderr/状态变更/Claude 消息)
- 单文件 SQLite, WAL 模式, 多协程并发安全靠 asyncio.Lock 串行写
"""
from __future__ import annotations

import asyncio
import json
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any, Iterable

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "secweb.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    description TEXT DEFAULT '',
    default_prompt TEXT DEFAULT '',
    concurrency INTEGER DEFAULT 3,
    auth_payload TEXT DEFAULT '',
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY,
    project_id TEXT,
    url TEXT NOT NULL,
    prompt TEXT NOT NULL,
    status TEXT NOT NULL,           -- queued/running/done/failed/stopped/needs_input
    executor_type TEXT DEFAULT 'claude_sdk',  -- 执行器类型 (claude_sdk 是 v2.0 唯一类型, 'claude' 是 v1.x 残留)
    session_id TEXT,                -- claude --session-id 复用
    created_at REAL NOT NULL,
    started_at REAL,
    finished_at REAL,
    exit_code INTEGER,
    pending_question TEXT,          -- 当 status=needs_input 时, Claude 的问题
    report_path TEXT,               -- 发现的报告文件相对路径
    has_finding INTEGER DEFAULT 0,  -- 是否产出报告/漏洞
    workdir TEXT NOT NULL,          -- runs/<id>
    pid INTEGER,                    -- 当前 claude 进程 pid (运行中)
    auth_payload TEXT DEFAULT '',   -- 任务级认证凭据 (cookie/token/Header), 留空则用 project.auth_payload 兜底
    mcp_calls INTEGER DEFAULT 0,           -- mcp__playwright__* 调用次数
    py_web_calls INTEGER DEFAULT 0,        -- Bash 内疑似 python/curl web 降级次数
    degraded_to_python INTEGER DEFAULT 0,  -- 1 = 该任务全程未用 playwright MCP
    cost_usd REAL DEFAULT 0,
    total_tokens INTEGER DEFAULT 0,
    duration_ms INTEGER DEFAULT 0,
    FOREIGN KEY(project_id) REFERENCES projects(id)
);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL,
    ts REAL NOT NULL,
    kind TEXT NOT NULL,             -- stdout/stderr/status/claude/system
    payload TEXT NOT NULL,
    FOREIGN KEY(task_id) REFERENCES tasks(id)
);

CREATE INDEX IF NOT EXISTS idx_events_task ON events(task_id, id);
CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
CREATE INDEX IF NOT EXISTS idx_tasks_project ON tasks(project_id);

CREATE TABLE IF NOT EXISTS app_settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at REAL NOT NULL
);
"""

_lock = asyncio.Lock()


def _conn() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(DB_PATH, timeout=30, isolation_level=None)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA synchronous=NORMAL")
    c.execute("PRAGMA foreign_keys=ON")
    return c


def init_db() -> None:
    with _conn() as c:
        c.executescript(_SCHEMA)
        # 老库迁移: 给 tasks 加 project_id 列
        cols = {r["name"] for r in c.execute("PRAGMA table_info(tasks)").fetchall()}
        if "project_id" not in cols:
            c.execute("ALTER TABLE tasks ADD COLUMN project_id TEXT")
            c.execute("CREATE INDEX IF NOT EXISTS idx_tasks_project ON tasks(project_id)")
        if "executor_type" not in cols:
            c.execute("ALTER TABLE tasks ADD COLUMN executor_type TEXT DEFAULT 'claude_sdk'")
        # v2.0 一次性迁移: 老数据 executor_type='claude' (子进程跑 CLI) 全部归到
        # SDK 执行器. executors REGISTRY 也保留 'claude'→ClaudeSDKExecutor 兼容映射.
        c.execute(
            "UPDATE tasks SET executor_type='claude_sdk' "
            "WHERE executor_type IS NULL OR executor_type='claude'"
        )
        # 老库迁移: 给 projects 加 concurrency 列 (项目级并发)
        pcols = {r["name"] for r in c.execute("PRAGMA table_info(projects)").fetchall()}
        if "concurrency" not in pcols:
            c.execute("ALTER TABLE projects ADD COLUMN concurrency INTEGER DEFAULT 3")
            c.execute("UPDATE projects SET concurrency=3 WHERE concurrency IS NULL")
        if "cookies" not in pcols and "auth_payload" not in pcols:
            c.execute("ALTER TABLE projects ADD COLUMN auth_payload TEXT DEFAULT ''")
        elif "cookies" in pcols and "auth_payload" not in pcols:
            # 历史列名 cookies -> auth_payload, 直接复制内容并保留旧列兼容
            c.execute("ALTER TABLE projects ADD COLUMN auth_payload TEXT DEFAULT ''")
            c.execute("UPDATE projects SET auth_payload = COALESCE(cookies,'')")
        # 新增列迁移: 工具用量 / 降级标记 / 成本统计
        for col, ddl in (
            ("mcp_calls", "ALTER TABLE tasks ADD COLUMN mcp_calls INTEGER DEFAULT 0"),
            ("py_web_calls", "ALTER TABLE tasks ADD COLUMN py_web_calls INTEGER DEFAULT 0"),
            ("degraded_to_python", "ALTER TABLE tasks ADD COLUMN degraded_to_python INTEGER DEFAULT 0"),
            ("cost_usd", "ALTER TABLE tasks ADD COLUMN cost_usd REAL DEFAULT 0"),
            ("total_tokens", "ALTER TABLE tasks ADD COLUMN total_tokens INTEGER DEFAULT 0"),
            ("duration_ms", "ALTER TABLE tasks ADD COLUMN duration_ms INTEGER DEFAULT 0"),
            ("login_pid", "ALTER TABLE tasks ADD COLUMN login_pid INTEGER"),
            ("mcp_pid", "ALTER TABLE tasks ADD COLUMN mcp_pid INTEGER"),
            ("mcp_port", "ALTER TABLE tasks ADD COLUMN mcp_port INTEGER"),
            # SPA 探索深度计量 (诊断"打卡式"浏览器使用, 见 runner._MIN_*)
            ("nav_calls", "ALTER TABLE tasks ADD COLUMN nav_calls INTEGER DEFAULT 0"),
            ("unique_routes", "ALTER TABLE tasks ADD COLUMN unique_routes INTEGER DEFAULT 0"),
            ("interaction_calls", "ALTER TABLE tasks ADD COLUMN interaction_calls INTEGER DEFAULT 0"),
            ("network_req_calls", "ALTER TABLE tasks ADD COLUMN network_req_calls INTEGER DEFAULT 0"),
            # Skill 契约执行状态 (来自 ~/.claude/skills/<name>/SKILL.md 抽取的强制门控,
            # 由 runner 收尾时根据 report.md 中 "## 契约执行清单" 段落计算)
            ("contract_skill", "ALTER TABLE tasks ADD COLUMN contract_skill TEXT"),
            ("contract_total", "ALTER TABLE tasks ADD COLUMN contract_total INTEGER DEFAULT 0"),
            ("contract_covered", "ALTER TABLE tasks ADD COLUMN contract_covered INTEGER DEFAULT 0"),
            ("contract_missing_json", "ALTER TABLE tasks ADD COLUMN contract_missing_json TEXT DEFAULT ''"),
            # 任务级认证凭据 (cookie / token / Authorization 等). 每次"新增目标"
            # 提交时存自己那批的 cookie, 互不影响; 留空则用 project.auth_payload 兜底
            ("auth_payload", "ALTER TABLE tasks ADD COLUMN auth_payload TEXT DEFAULT ''"),
        ):
            if col not in cols:
                c.execute(ddl)
        # 启动时把残留的 running/needs_input/paused 任务标记为 stopped (上次未正常退出)
        # 同时清理 mcp_pid/mcp_port: 上次 daemon 已随主进程退出, 复活的孤儿先扫掉
        # 由 scheduler.startup_cleanup 调用 browser_daemon.kill_daemon 收尾
        c.execute(
            "UPDATE tasks SET status='stopped', finished_at=?, pid=NULL "
            "WHERE status IN ('running','needs_input','paused','awaiting_login')",
            (time.time(),),
        )


# ---------- 项目 ----------

async def create_project(name: str, description: str = "", default_prompt: str = "", concurrency: int = 3, auth_payload: str = "") -> str:
    pid = uuid.uuid4().hex[:12]
    async with _lock:
        with _conn() as c:
            c.execute(
                "INSERT INTO projects(id,name,description,default_prompt,concurrency,auth_payload,created_at) "
                "VALUES(?,?,?,?,?,?,?)",
                (pid, name, description, default_prompt, max(1, min(concurrency, 32)), auth_payload, time.time()),
            )
    return pid


async def update_project(pid: str, **fields: Any) -> None:
    if not fields:
        return
    cols = ", ".join(f"{k}=?" for k in fields)
    vals = list(fields.values()) + [pid]
    async with _lock:
        with _conn() as c:
            c.execute(f"UPDATE projects SET {cols} WHERE id=?", vals)


def get_project(pid: str) -> dict | None:
    with _conn() as c:
        row = c.execute("SELECT * FROM projects WHERE id=?", (pid,)).fetchone()
        return dict(row) if row else None


def list_projects() -> list[dict]:
    with _conn() as c:
        rows = c.execute(
            """
            SELECT p.*,
              (SELECT COUNT(*) FROM tasks WHERE project_id=p.id) AS task_count,
              (SELECT COUNT(*) FROM tasks WHERE project_id=p.id AND status='running') AS running_count,
              (SELECT COUNT(*) FROM tasks WHERE project_id=p.id AND status='queued') AS queued_count,
              (SELECT COUNT(*) FROM tasks WHERE project_id=p.id AND status='needs_input') AS needs_input_count,
              (SELECT COUNT(*) FROM tasks WHERE project_id=p.id AND status='done') AS done_count,
              (SELECT COUNT(*) FROM tasks WHERE project_id=p.id AND status='failed') AS failed_count,
              (SELECT COUNT(*) FROM tasks WHERE project_id=p.id AND status='stopped') AS stopped_count,
              (SELECT COUNT(*) FROM tasks WHERE project_id=p.id AND has_finding=1) AS finding_count,
              (SELECT MAX(created_at) FROM tasks WHERE project_id=p.id) AS last_active,
              (SELECT url FROM tasks WHERE project_id=p.id AND has_finding=1
                 ORDER BY finished_at DESC LIMIT 1) AS latest_finding_url,
              (SELECT url FROM tasks WHERE project_id=p.id AND status='running'
                 ORDER BY started_at DESC LIMIT 1) AS latest_running_url
            FROM projects p
            ORDER BY p.created_at DESC
            """
        ).fetchall()
        return [dict(r) for r in rows]


def list_reports(project_id: str | None = None, limit: int = 500) -> list[dict]:
    """聚合报告: 列出所有 has_finding=1 且 report_path 不为空的任务."""
    where = ["has_finding=1", "report_path IS NOT NULL", "report_path != ''"]
    params: list = []
    if project_id is not None:
        where.append("project_id=?")
        params.append(project_id)
    params.append(limit)
    sql = (
        "SELECT t.id, t.project_id, t.url, t.report_path, t.workdir, "
        "t.finished_at, t.created_at, t.status, p.name AS project_name "
        "FROM tasks t LEFT JOIN projects p ON t.project_id=p.id "
        f"WHERE {' AND '.join(where)} "
        "ORDER BY COALESCE(t.finished_at, t.created_at) DESC LIMIT ?"
    )
    with _conn() as c:
        return [dict(r) for r in c.execute(sql, params).fetchall()]


async def delete_project(pid: str, cascade: bool = False) -> int:
    """删除项目。cascade=True 时同时删项目下所有任务及事件。返回受影响任务数。"""
    async with _lock:
        with _conn() as c:
            if cascade:
                tids = [r["id"] for r in c.execute(
                    "SELECT id FROM tasks WHERE project_id=?", (pid,)
                ).fetchall()]
                for tid in tids:
                    c.execute("DELETE FROM events WHERE task_id=?", (tid,))
                c.execute("DELETE FROM tasks WHERE project_id=?", (pid,))
                c.execute("DELETE FROM projects WHERE id=?", (pid,))
                return len(tids)
            else:
                # 不级联: 仅当项目没有任务时才能删
                n = c.execute(
                    "SELECT COUNT(*) AS n FROM tasks WHERE project_id=?", (pid,)
                ).fetchone()["n"]
                if n > 0:
                    return -1
                c.execute("DELETE FROM projects WHERE id=?", (pid,))
                return 0


# ---------- 任务 ----------

async def create_task(
    url: str, prompt: str, workdir: str,
    project_id: str | None = None, auth_payload: str = "",
) -> str:
    tid = uuid.uuid4().hex[:12]
    sid = str(uuid.uuid4())  # claude --session-id 必须是 UUID
    async with _lock:
        with _conn() as c:
            c.execute(
                "INSERT INTO tasks(id,project_id,url,prompt,status,session_id,"
                "created_at,workdir,auth_payload) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (tid, project_id, url, prompt, "queued", sid, time.time(), workdir, auth_payload),
            )
    return tid


async def update_task(tid: str, **fields: Any) -> None:
    if not fields:
        return
    cols = ", ".join(f"{k}=?" for k in fields)
    vals = list(fields.values()) + [tid]
    async with _lock:
        with _conn() as c:
            c.execute(f"UPDATE tasks SET {cols} WHERE id=?", vals)


def get_task(tid: str) -> dict | None:
    with _conn() as c:
        row = c.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()
        return dict(row) if row else None


def list_tasks(
    status: str | None = None,
    project_id: str | None = None,
    limit: int = 500,
) -> list[dict]:
    where = []
    params: list = []
    if status:
        where.append("status=?")
        params.append(status)
    if project_id is not None:
        where.append("project_id=?")
        params.append(project_id)
    sql = "SELECT * FROM tasks"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY created_at DESC LIMIT ?"
    params.append(limit)
    with _conn() as c:
        return [dict(r) for r in c.execute(sql, params).fetchall()]


def stats(project_id: str | None = None) -> dict:
    with _conn() as c:
        if project_id is not None:
            rows = c.execute(
                "SELECT status, COUNT(*) AS n FROM tasks WHERE project_id=? GROUP BY status",
                (project_id,),
            ).fetchall()
            findings = c.execute(
                "SELECT COUNT(*) AS n FROM tasks WHERE project_id=? AND has_finding=1",
                (project_id,),
            ).fetchone()["n"]
        else:
            rows = c.execute(
                "SELECT status, COUNT(*) AS n FROM tasks GROUP BY status"
            ).fetchall()
            findings = c.execute(
                "SELECT COUNT(*) AS n FROM tasks WHERE has_finding=1"
            ).fetchone()["n"]
        out = {r["status"]: r["n"] for r in rows}
        out["findings"] = findings
        return out


async def delete_task(tid: str) -> None:
    async with _lock:
        with _conn() as c:
            c.execute("DELETE FROM events WHERE task_id=?", (tid,))
            c.execute("DELETE FROM tasks WHERE id=?", (tid,))


# ---------- 事件 ----------

async def add_event(task_id: str, kind: str, payload: str | dict) -> int:
    if isinstance(payload, dict):
        payload = json.dumps(payload, ensure_ascii=False)
    async with _lock:
        with _conn() as c:
            cur = c.execute(
                "INSERT INTO events(task_id,ts,kind,payload) VALUES(?,?,?,?)",
                (task_id, time.time(), kind, payload),
            )
            return cur.lastrowid


def get_events(task_id: str, after_id: int = 0, limit: int = 2000) -> list[dict]:
    with _conn() as c:
        rows = c.execute(
            "SELECT * FROM events WHERE task_id=? AND id>? ORDER BY id ASC LIMIT ?",
            (task_id, after_id, limit),
        ).fetchall()
        return [dict(r) for r in rows]


def task_ids_iter() -> Iterable[str]:
    with _conn() as c:
        for r in c.execute("SELECT id FROM tasks").fetchall():
            yield r["id"]


# ---------- 应用级设置 (key/value JSON) ----------

def get_setting(key: str) -> dict | None:
    with _conn() as c:
        row = c.execute(
            "SELECT value FROM app_settings WHERE key=?", (key,)
        ).fetchone()
        if not row:
            return None
        try:
            return json.loads(row["value"])
        except Exception:
            return None


async def set_setting(key: str, value: dict) -> None:
    payload = json.dumps(value, ensure_ascii=False)
    async with _lock:
        with _conn() as c:
            c.execute(
                "INSERT INTO app_settings(key,value,updated_at) VALUES(?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                (key, payload, time.time()),
            )
