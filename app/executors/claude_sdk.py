"""Claude SDK 执行器 - secweb v2.0 唯一执行器.

历史: 早期有 ClaudeExecutor (claude.py, subprocess 跑 claude CLI), 后改为 SDK
in-process. v2.0 完全退役 CLI 路径, 老任务的 executor_type='claude' 也走本类.

业务逻辑全部在 app/runner_sdk.py + app/guard_state.py, 此处只做调度层接入薄壳.
"""
from __future__ import annotations

from .. import runner_sdk, store
from .base import ExecutorContext, ExecutorResult, TaskExecutor


class ClaudeSDKExecutor:
    """通过 Claude Agent SDK 跑任务的执行器."""

    type: str = "claude_sdk"

    async def run(self, task: dict, ctx: ExecutorContext) -> ExecutorResult:
        resume = bool(ctx.extra.get("resume", False))
        extra_input = ctx.extra.get("extra_input")
        await runner_sdk.run_task(
            task["id"],
            claude_bin=ctx.claude_bin,  # SDK 不用此值, 兼容签名
            timeout=ctx.timeout,
            resume=resume,
            extra_input=extra_input,
            on_event=ctx.on_event,
        )
        t = store.get_task(task["id"]) or task
        return ExecutorResult(
            status=t.get("status", "done"),
            exit_code=t.get("exit_code"),
            report_path=t.get("report_path"),
            has_finding=bool(t.get("has_finding")),
        )
