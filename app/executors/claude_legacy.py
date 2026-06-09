"""Claude CLI 执行器 - 把现有的 runner.run_task 包装成 TaskExecutor。

这是渐进式重构: runner.py 内部实现暂保留, 此处只做适配层。
后续可以把 runner.py 的逻辑搬进来, 也可以保留以兼容老调用。
"""
from __future__ import annotations

from .. import runner, store
from .base import ExecutorContext, ExecutorResult, TaskExecutor


class ClaudeExecutor:
    """通过 claude CLI 跑任务的执行器。"""

    type: str = "claude"

    async def run(self, task: dict, ctx: ExecutorContext) -> ExecutorResult:
        resume = bool(ctx.extra.get("resume", False))
        extra_input = ctx.extra.get("extra_input")
        await runner.run_task(
            task["id"],
            claude_bin=ctx.claude_bin,
            timeout=ctx.timeout,
            resume=resume,
            extra_input=extra_input,
            on_event=ctx.on_event,
        )
        # runner.run_task 内部已经写了 status/exit_code, 这里读最新一手
        t = store.get_task(task["id"]) or task
        return ExecutorResult(
            status=t.get("status", "done"),
            exit_code=t.get("exit_code"),
            report_path=t.get("report_path"),
            has_finding=bool(t.get("has_finding")),
        )
