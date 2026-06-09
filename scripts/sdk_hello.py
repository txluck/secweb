"""Phase 1 hello world: 验证 Claude Agent SDK + packyapi base_url 工作.

跑完应该看到:
- system/init: session_id 出来
- assistant.text: 模型回复
- result: subtype=success, num_turns=1

环境变量从 ccswitch 继承 (ANTHROPIC_BASE_URL / ANTHROPIC_AUTH_TOKEN).
"""
import asyncio
import os
import sys

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    ResultMessage,
    SystemMessage,
    TextBlock,
    ThinkingBlock,
    ToolUseBlock,
    UserMessage,
)


def render(msg) -> None:
    """打印 SDK 消息, 简洁格式."""
    cls = type(msg).__name__
    if isinstance(msg, SystemMessage):
        keys = list((msg.data or {}).keys())[:5]
        print(f"[system/{msg.subtype}] keys={keys}")
        if msg.subtype == "init":
            sid = (msg.data or {}).get("session_id")
            print(f"  session_id: {sid}")
    elif isinstance(msg, AssistantMessage):
        for blk in msg.content:
            if isinstance(blk, TextBlock):
                txt = blk.text[:200].replace("\n", "\\n")
                print(f"[assistant.text] {txt}")
            elif isinstance(blk, ToolUseBlock):
                print(f"[assistant.tool_use] {blk.name} input={str(blk.input)[:120]}")
            elif isinstance(blk, ThinkingBlock):
                print(f"[assistant.thinking] {blk.thinking[:120]}")
    elif isinstance(msg, UserMessage):
        print(f"[{cls}] content={str(msg.content)[:100]}")
    elif isinstance(msg, ResultMessage):
        print(
            f"[result] subtype={msg.subtype} turns={msg.num_turns} "
            f"cost=${msg.total_cost_usd} stop_reason={msg.stop_reason} "
            f"is_error={msg.is_error}"
        )
    else:
        print(f"[{cls}] {msg!r}")


async def main():
    print("=== ENV CHECK ===")
    for k in ("ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY"):
        v = os.environ.get(k, "")
        if v:
            print(f"  {k}={'*' * 8 if 'TOKEN' in k or 'KEY' in k else v}")
        else:
            print(f"  {k}=(unset)")
    print()

    options = ClaudeAgentOptions(
        system_prompt={
            "type": "preset",
            "preset": "claude_code",
            "append": "回复必须中文, 一句话内.",
        },
        permission_mode="bypassPermissions",
        allowed_tools=[],  # hello world 不用工具
        # ANTHROPIC_BASE_URL / ANTHROPIC_AUTH_TOKEN 从父进程 env 继承
        # cwd=tempfile  # 不指定时 SDK 用进程 cwd
    )

    print("=== START ===")
    async with ClaudeSDKClient(options=options) as client:
        await client.query("说 hello")
        async for msg in client.receive_response():
            render(msg)
    print("=== DONE ===")


if __name__ == "__main__":
    asyncio.run(main())
