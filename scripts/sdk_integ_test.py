"""Phase 3 集成测试: runner_sdk.run_task 完整跑通一次模拟任务.

验证:
- store.create_task 创建任务
- runner_sdk.run_task 跑 SDK
- DB status 更新 (running → done/failed)
- .tool-events.jsonl 写入
- on_event 回调被调用
"""
import asyncio
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import store, runner_sdk


async def on_event(task_id: str, kind: str, payload):
    """打印事件 (代替 WebSocket emit)."""
    if kind == "thinking":
        return  # 长 thinking 不打印
    info = str(payload)[:120].replace("\n", "\\n")
    print(f"  [{kind}] {info}")


async def main():
    # 准备 workdir
    workdir = Path(tempfile.mkdtemp(prefix="sdk_integ_"))
    print(f"workdir: {workdir}")

    # 创建任务 (模拟 dashboard 提交)
    task_id = await store.create_task(
        url="https://example.com",
        prompt="跑一次 'echo hello-from-sdk', 然后说 done.",
        workdir=str(workdir),
    )
    print(f"task_id: {task_id}")

    task = store.get_task(task_id)
    print(f"initial status: {task['status']}")
    print(f"session_id: {task['session_id']}")
    print()

    # 跑 runner_sdk
    print("=== START run_task ===")
    await runner_sdk.run_task(
        task_id,
        claude_bin="/usr/local/bin/claude",  # 兼容签名, SDK 不用
        timeout=120,
        resume=False,
        extra_input=None,
        on_event=on_event,
    )
    print("=== END run_task ===")
    print()

    # 检查最终状态
    task = store.get_task(task_id)
    print(f"final status: {task['status']}")
    print(f"exit_code: {task.get('exit_code')}")
    print(f"report_path: {task.get('report_path')}")
    print(f"has_finding: {task.get('has_finding')}")
    print(f"mcp_calls: {task.get('mcp_calls')}")
    print(f"bash_calls: {task.get('bash_calls')}")

    # .tool-events.jsonl 验证
    tool_events = workdir / ".tool-events.jsonl"
    print()
    print(f"=== {tool_events.name} ===")
    if tool_events.exists():
        lines = tool_events.read_text().splitlines()
        print(f"  行数: {len(lines)}")
        import json
        for i, line in enumerate(lines[:3]):
            ev = json.loads(line)
            print(f"  [{i}] tool={ev.get('tool_name')} input={str(ev.get('tool_input',''))[:80]}")
    else:
        print("  ✗ 文件不存在")


if __name__ == "__main__":
    asyncio.run(main())
