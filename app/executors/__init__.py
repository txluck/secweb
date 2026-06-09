"""执行器层 - 把 "一次任务" 抽象成可插拔的 Executor。

v2.0: 唯一执行器是 ClaudeSDKExecutor (claude-agent-sdk in-process).
老 ClaudeExecutor (子进程跑 claude CLI) 已下线, 但保留 'claude' → ClaudeSDKExecutor
的映射以兼容历史 DB 行.

为后续模块化扩展 (资产监控/主动收集/Nuclei 扫描等) 留扩展点:
新增执行器 = 实现 TaskExecutor 协议 + 在 REGISTRY 里注册一个类型.
"""
from __future__ import annotations

from .base import TaskExecutor, ExecutorContext, ExecutorResult
from .claude_sdk import ClaudeSDKExecutor

REGISTRY: dict[str, type[TaskExecutor]] = {
    "claude_sdk": ClaudeSDKExecutor,
    # 老 DB 行的 executor_type='claude' 仍指向 SDK 执行器 (兼容性映射)
    "claude": ClaudeSDKExecutor,
}


def get_executor(executor_type: str) -> type[TaskExecutor]:
    """根据类型取执行器类. 未知类型默认 ClaudeSDKExecutor."""
    return REGISTRY.get(executor_type, ClaudeSDKExecutor)


__all__ = [
    "TaskExecutor",
    "ExecutorContext",
    "ExecutorResult",
    "ClaudeSDKExecutor",
    "REGISTRY",
    "get_executor",
]
