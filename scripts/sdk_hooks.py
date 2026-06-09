"""Phase 2: PostToolUse hook 落 .tool-events.jsonl + PreToolUse hook 阻断 TodoWrite.

目标:
1. 让模型调一次 Bash 工具
2. PostToolUse hook 写入 workdir/.tool-events.jsonl (兼容 v1.2.2 的解析逻辑)
3. PreToolUse hook 拦 TodoWrite (返回 deny)
4. Stop hook 看到调用过 Bash 后放行
"""
import asyncio
import json
import os
import tempfile
from datetime import datetime
from pathlib import Path

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    HookMatcher,
    ResultMessage,
    SystemMessage,
    TextBlock,
    ToolUseBlock,
    UserMessage,
)


def make_hooks(workdir: Path):
    """构造与 v1.2.2 兼容的 hook 三件套."""
    tool_events_path = workdir / ".tool-events.jsonl"

    async def post_tool_log(input_data, tool_use_id, context):
        """PostToolUse: 等价 v1.2.2 的 'cat >> .tool-events.jsonl'."""
        try:
            entry = {
                "ts": datetime.utcnow().isoformat() + "Z",
                "session_id": input_data.get("session_id"),
                "cwd": input_data.get("cwd"),
                "tool_name": input_data.get("tool_name"),
                "tool_input": input_data.get("tool_input"),
                "tool_response": input_data.get("tool_response"),
                "tool_use_id": tool_use_id,
            }
            tool_events_path.parent.mkdir(parents=True, exist_ok=True)
            with tool_events_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except Exception as e:
            print(f"[hook.post] write error: {e}")
        return {}

    async def pre_block_todowrite(input_data, tool_use_id, context):
        """PreToolUse: 拦 TodoWrite (验证 deny 机制)."""
        if input_data.get("tool_name") == "TodoWrite":
            print(f"[hook.pre] DENY TodoWrite")
            return {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason":
                        "TodoWrite is disabled in this Phase 2 demo.",
                },
            }
        return {}

    async def stop_check(input_data, tool_use_id, context):
        """Stop: 看是否调用过工具 (与 v1.2.2 Layer 2 等价的简化版)."""
        if not tool_events_path.exists():
            print(f"[hook.stop] BLOCK: no tool events")
            return {
                "decision": "block",
                "reason": "你还没调用任何工具, 必须先用 Bash 跑一个命令再退出.",
            }
        # 检查事件数
        n = sum(1 for _ in tool_events_path.open(encoding="utf-8"))
        print(f"[hook.stop] events={n}, allowing exit")
        return {}

    return {
        "PostToolUse": [HookMatcher(hooks=[post_tool_log])],
        "PreToolUse": [HookMatcher(matcher="TodoWrite", hooks=[pre_block_todowrite])],
        "Stop": [HookMatcher(hooks=[stop_check])],
    }


def render(msg):
    cls = type(msg).__name__
    if isinstance(msg, SystemMessage):
        if msg.subtype == "init":
            sid = (msg.data or {}).get("session_id")
            print(f"[system.init] session={sid}")
        else:
            print(f"[system.{msg.subtype}]")
    elif isinstance(msg, AssistantMessage):
        for blk in msg.content:
            if isinstance(blk, TextBlock):
                print(f"[assistant.text] {blk.text[:160]}")
            elif isinstance(blk, ToolUseBlock):
                print(f"[assistant.tool_use] {blk.name}")
    elif isinstance(msg, UserMessage):
        if isinstance(msg.content, list):
            for blk in msg.content:
                if isinstance(blk, dict) and blk.get("type") == "tool_result":
                    is_err = blk.get("is_error", False)
                    print(f"[user.tool_result] is_error={is_err}")
    elif isinstance(msg, ResultMessage):
        print(
            f"[result] subtype={msg.subtype} turns={msg.num_turns} "
            f"cost=${msg.total_cost_usd} is_error={msg.is_error}"
        )


async def main():
    workdir = Path(tempfile.mkdtemp(prefix="sdk_hook_"))
    print(f"=== workdir: {workdir} ===")

    options = ClaudeAgentOptions(
        system_prompt={
            "type": "preset",
            "preset": "claude_code",
            "append": "用 Bash 工具运行 'echo hello-from-bash' 然后退出.",
        },
        permission_mode="bypassPermissions",
        allowed_tools=["Bash", "TodoWrite"],
        cwd=str(workdir),
        hooks=make_hooks(workdir),
        # 不加载用户级 settings.json (隔离测试)
        setting_sources=[],
    )

    print("=== START ===")
    async with ClaudeSDKClient(options=options) as client:
        await client.query("用 Bash 跑 'echo hello-from-bash', 然后说 done.")
        async for msg in client.receive_response():
            render(msg)
    print("=== DONE ===")

    # 验证 .tool-events.jsonl
    tool_events = workdir / ".tool-events.jsonl"
    print()
    print("=== 验证 .tool-events.jsonl ===")
    if tool_events.exists():
        lines = tool_events.read_text().splitlines()
        print(f"  行数: {len(lines)}")
        for i, line in enumerate(lines[:5]):
            ev = json.loads(line)
            print(f"  [{i}] tool={ev.get('tool_name')} cmd={(str(ev.get('tool_input',{}).get('command','')) or str(ev.get('tool_input','')))[:80]}")
    else:
        print("  ✗ 文件不存在!")


if __name__ == "__main__":
    asyncio.run(main())
