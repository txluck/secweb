"""TaskExecutor 协议 - 所有任务执行器的统一接口。

每个 executor 接收: task 字典 + 上下文 (claude_bin/runs_dir/timeout/事件回调)
返回: ExecutorResult (status/exit_code/extra)
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Protocol

EventCB = Callable[[str, str, Any], Awaitable[None]]


@dataclass
class ExecutorContext:
    """执行器拿到的运行时上下文 (从 Scheduler 注入)。"""
    claude_bin: str
    runs_dir: Path
    timeout: int
    on_event: EventCB | None = None
    # 调度器额外参数 (resume/extra_input 等)
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class ExecutorResult:
    """执行结束后的标准返回。"""
    status: str  # done / failed / stopped / needs_input
    exit_code: int | None = None
    report_path: str | None = None
    has_finding: bool = False
    extra: dict[str, Any] = field(default_factory=dict)


class TaskExecutor(Protocol):
    """所有任务执行器必须实现的协议。

    type:        执行器类型字符串, 与 task.executor_type 对应
    run:         异步执行入口
    """

    type: str

    async def run(self, task: dict, ctx: ExecutorContext) -> ExecutorResult:
        ...
