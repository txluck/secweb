"""数据模型 - dataclass 集中定义所有持久化实体。

设计目标:
- 所有表的字段定义集中在此, store.py 只做 SQL 适配
- 提供 from_row / to_dict 工具方法, 与 sqlite3.Row 互转
- 后续新增模块 (资产/调度) 在此追加 dataclass 即可
"""
from __future__ import annotations

import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any


def _new_id(n: int = 12) -> str:
    return uuid.uuid4().hex[:n]


def _now() -> float:
    return time.time()


@dataclass
class Project:
    id: str = field(default_factory=_new_id)
    name: str = ""
    description: str = ""
    default_prompt: str = ""
    created_at: float = field(default_factory=_now)

    @classmethod
    def from_row(cls, row: Any) -> "Project":
        return cls(**{k: row[k] for k in row.keys() if k in cls.__dataclass_fields__})

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Task:
    id: str = field(default_factory=lambda: _new_id(12))
    project_id: str | None = None
    url: str = ""
    prompt: str = ""
    status: str = "queued"
    executor_type: str = "claude"
    session_id: str | None = None
    created_at: float = field(default_factory=_now)
    started_at: float | None = None
    finished_at: float | None = None
    exit_code: int | None = None
    pending_question: str | None = None
    report_path: str | None = None
    has_finding: int = 0
    workdir: str = ""
    pid: int | None = None

    # 任务状态机定义
    TERMINAL_STATUSES = ("done", "failed", "stopped")
    ACTIVE_STATUSES = ("running", "queued")
    RESUMABLE_STATUSES = ("done", "failed", "stopped")

    @classmethod
    def from_row(cls, row: Any) -> "Task":
        return cls(**{k: row[k] for k in row.keys() if k in cls.__dataclass_fields__})

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Event:
    """任务执行过程中产生的事件 (stdout/stderr/status/claude_text/tool_use/...)"""
    id: int | None = None
    task_id: str = ""
    ts: float = field(default_factory=_now)
    kind: str = ""
    payload: str = ""

    @classmethod
    def from_row(cls, row: Any) -> "Event":
        return cls(**{k: row[k] for k in row.keys() if k in cls.__dataclass_fields__})

    def to_dict(self) -> dict:
        return asdict(self)


__all__ = ["Project", "Task", "Event"]
