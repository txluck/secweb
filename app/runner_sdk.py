"""SDK 版 runner — secweb v2.0 唯一 runner.

设计原则 (从 v1.x 缝补式架构提炼):
- 一份 metric 源 (state.metrics, 来自 SDK PostToolUse hook in-process 回调)
- 一份 guard 实现 (app.guard_state 纯 Python 函数, 不再有子进程 / marker 文件)
- 显式循环代替递归 (case_a/b/c/d 早停续跑用 while attempt < MAX 表达, 不再
  自递归 run_task)
- 同一 ClaudeSDKClient 复用 (playwright MCP 浏览器全程不掉, followup 也在同 client 上 query)

行为零回归 (与 v1.x runner.py 对比):
- 6 层守卫 (Layer 1/2/3/4/5/6) — 通过 guard_state.check_* + state 更新保留
- SPA 探索深度阈值 (case_b) — metrics.is_browser_shallow() 替代
- 契约执行 (case_c) — skill_contract.parse_report_contract_status 保留
- Phase 审计 (case_d) — guard_state.audit_phases 替代 phase_audit.audit
- degraded 紫标 — metrics.is_degraded() + state.fuzz_shallow / soft_skipped / retro_shallow
- 中文报告检测 — _scan_report_lang() 保留
- 邮件通知 — finalize 末尾保留

不再保留 (SDK in-process 模式天然不需要):
- silence_watchdog (没有 stdout pipe 卡死场景, SDK 直连 anthropic)
- 1M ctx compaction 特殊处理 (SDK 自身处理)
- keep_alive / inject_user_message stdin 协议 (改用 client.query)
- SIGSTOP/SIGCONT (pause 改为发 query 节流, 详见 scheduler)
"""
from __future__ import annotations

import asyncio
import json
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    HookMatcher,
    ResultMessage,
    SystemMessage,
    TextBlock,
    ThinkingBlock,
    ToolUseBlock,
    UserMessage,
)

from . import store
from .guard_state import (
    GuardState,
    Allow,
    Deny,
    Block,
    audit_phases,
    build_phase_skip_nudge,
    check_pretool_skill,
    check_pretool_todowrite,
    check_stop,
    decision_to_sdk_pretool,
    decision_to_sdk_stop,
    observe_tool,
)
from .mcp_config import build_task_mcp_config
from .skill_contract import (
    build_contract_prompt,
    build_skill_invocation_prefix,
    detect_slash_skill,
    extract_contract,
    parse_report_contract_status,
)
from .system_prompt import NEEDS_INPUT_PREFIX, system_append

EventCB = Callable[[str, str, Any], Awaitable[None]]


# 允许的工具集 (与旧 runner._ALLOWED_TOOLS 一致, 含 playwright/jadx MCP)
#
# 不变量 (由 _validate_tool_config 启动期断言守护):
#   凡是 _make_hooks 里 `tool_name == "X"` 被特殊接管的 X,
#   必须出现在本白名单. 漏配会被 SDK 默默过滤, 导致 hook 链路空跑.
#
# 历史事故 (2026-06-04): 漏配 "Skill" → 五个 dashboard 任务全产出
# `EXEMPT-FULL: Skill 返回 Unknown skill` 假豁免报告, 整条 hack 流水线被绕过.
_ALLOWED_TOOLS = [
    "Bash", "Read", "Write", "Edit", "Grep", "Glob",
    "WebFetch", "WebSearch", "TaskCreate", "TaskUpdate", "TaskList",
    "Skill",
    "mcp__playwright__*",
    "mcp__jadx-mcp-server__*",
]


def _validate_tool_config() -> None:
    """启动期断言: _ALLOWED_TOOLS 必须覆盖 _make_hooks 里所有显式拦截的工具.

    防御策略: AST 解析自身源码 (不会误抓注释/docstring 里的示例字符串),
    模块 import 阶段就触发, 比单元测试 / 启动后检测 / Stop hook 兜底都更早 —
    进程根本起不来.

    历史事故 (2026-06-04, 五份 EXEMPT-FULL 报告):
      _make_hooks PreToolUse 里写 `if tool_name == "Skill": ...`,
      system_append() 反复强调 "没有 Skill(...) 工具调用记录就等于没有执行该 skill",
      但 _ALLOWED_TOOLS 漏了 "Skill" → SDK 默默过滤 → AI 调不通 →
      编 EXEMPT-FULL 借口跳过整条 hack 流水线 →
      validate 7 问门 / 绝对不报清单 / 铁律 1-7 全失效 →
      报告全是 SourceMap/CORS/速率限制 类绝对不报项.
    """
    import ast

    src = Path(__file__).read_text(encoding="utf-8")
    tree = ast.parse(src)

    # 扫所有 `tool_name == "X"` 比较表达式 (AST 不看注释/docstring)
    hook_handled: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Compare):
            continue
        left = node.left
        if not (isinstance(left, ast.Name) and left.id == "tool_name"):
            continue
        for op, right in zip(node.ops, node.comparators):
            if (
                isinstance(op, ast.Eq)
                and isinstance(right, ast.Constant)
                and isinstance(right.value, str)
            ):
                hook_handled.add(right.value)

    # 通配符 (mcp__*) 不进具体集合 — 此类工具由 MCP 服务器侧管理, 不走 hook 路径
    allowed_concrete = {t for t in _ALLOWED_TOOLS if "*" not in t}

    # 已知 SDK preset:claude_code 默认带的内置工具 (无需在白名单里也可调)
    # TodoWrite 是 Claude Code 内置 todo 工具, allowed_tools 不含也能用
    _SDK_BUILTIN = {"TodoWrite"}

    missing = hook_handled - allowed_concrete - _SDK_BUILTIN
    if missing:
        raise RuntimeError(
            f"runner_sdk._ALLOWED_TOOLS 漏配工具 {sorted(missing)} - "
            f"_make_hooks 已为这些工具准备 PreToolUse 拦截, 但白名单没列入. "
            f"SDK 会默默过滤掉, hook 链路空跑, AI 调用失败编借口跳流水线. "
            f"修复: 把缺失项加进 _ALLOWED_TOOLS. "
            f"参考 2026-06-04 五份 EXEMPT-FULL 假豁免报告事故."
        )


_validate_tool_config()


# 自动续跑上限 (取代旧 _AUTO_RESUMED 进程级 dict, 现作 per-task 计数)
_MAX_RESUME_ATTEMPTS = 2  # 第一轮 + 最多两次续, 共三轮

_TOOL_EVENTS_FILE = ".tool-events.jsonl"


# ───── 调度器持的活跃 client 注册表 (供 followup / stop 复用) ──────
# key: task_id, value: dict(client=ClaudeSDKClient, state=GuardState, paused_event=asyncio.Event)
_ACTIVE_CLIENTS: dict[str, dict] = {}


def get_active_client(task_id: str):
    """scheduler 用 — 取当前活跃 SDK client (followup 复用 session)."""
    rec = _ACTIVE_CLIENTS.get(task_id)
    return rec.get("client") if rec else None


def get_active_state(task_id: str):
    """scheduler 用 — 取当前活跃 GuardState (用于 pause/unpause)."""
    rec = _ACTIVE_CLIENTS.get(task_id)
    return rec.get("state") if rec else None


def is_paused(task_id: str) -> bool:
    rec = _ACTIVE_CLIENTS.get(task_id)
    return bool(rec and rec.get("state") and rec["state"].paused)


async def set_paused(task_id: str, on: bool) -> bool:
    """scheduler.pause_task / unpause_task 用. 返回是否生效.

    pause 实现 (SDK 化的正确做法):
    - on=True: state.paused=True + await client.interrupt() 中断当前 turn
      模型立刻停下, playwright-mcp 子进程仍活 (浏览器窗口在, 可以手动登录),
      pump_messages 收到 ResultMessage(stop_reason='interrupted') 后返回,
      run_task 主循环检测到 state.paused 后 await pause_event 等 unpause
    - on=False: state.paused=False + pause_event.set()
      主循环醒来后用 client.query('继续上一步未完成的工作') 重启新 turn
    """
    rec = _ACTIVE_CLIENTS.get(task_id)
    if not rec:
        return False
    state = rec.get("state")
    pause_event = rec.get("pause_event")
    client = rec.get("client")
    if not state or not pause_event:
        return False
    if on:
        state.paused = True
        pause_event.clear()
        # 中断当前 turn — 让模型立刻停下读浏览器, 用户能干净地接管手动登录
        # 如果 client.interrupt() 没暴露或失败, 仍然走"等当前 turn 自然结束"路径
        if client is not None:
            try:
                await client.interrupt()
            except Exception:
                pass  # interrupt 不可达不算 fatal, 主循环检测 paused 仍能停
    else:
        state.paused = False
        pause_event.set()
    return True


def is_paused_sync(task_id: str) -> bool:
    """同步版查询 (HTTP 路由用)."""
    return is_paused(task_id)


# ───── Emit / DB 辅助 ──────────────────────────────────────────

async def _emit(task_id: str, kind: str, payload: Any, cb: EventCB | None) -> None:
    """事件持久化 (DB) + WebSocket 广播.

    顺序: 先 WS 推 (前端立即收到, 实时日志不卡), 再 DB 写 (持久化, 慢一些不影响 UI).
    历史 bug: 先 DB 后 WS, SQLite 全局锁导致每条事件串行 30-100ms,
    高峰期事件队列累积几秒到几分钟, 前端实时日志严重滞后.
    """
    if cb:
        await cb(task_id, kind, payload)
    await store.add_event(task_id, kind, payload)


def _truncate(s: str, n: int = 4000) -> str:
    if len(s) <= n:
        return s
    return s[:n] + f"\n…[truncated {len(s)-n} chars]"


def _scan_report(workdir: Path) -> str | None:
    """挑出最像最终报告的 markdown 文件 (相对路径). 与 runner.py 旧实现一致."""
    if not workdir.exists():
        return None
    candidates: list[tuple[int, Path]] = []
    for p in workdir.rglob("*.md"):
        name = p.name.lower()
        if name == "report.md":
            score = 100
        elif "report" in name:
            score = 50
        elif "finding" in name or "vuln" in name:
            score = 30
        else:
            score = 1
        try:
            score += min(p.stat().st_size // 200, 30)
        except OSError:
            pass
        candidates.append((score, p))
    if not candidates:
        return None
    candidates.sort(reverse=True)
    best = candidates[0][1]
    if candidates[0][0] < 5:
        return None
    return str(best.relative_to(workdir))


def _report_is_chinese(workdir: Path, report_rel: str) -> bool:
    """检测 report.md CJK 占比, 太低 (< 15%) 返回 False, 触发 degraded 紫标."""
    try:
        text = (workdir / report_rel).read_text(encoding="utf-8", errors="ignore")
        stripped = re.sub(r"```.*?```", "", text, flags=re.DOTALL)
        stripped = re.sub(r"`[^`]+`", "", stripped)
        cjk = sum(1 for ch in stripped if "一" <= ch <= "鿿")
        visible = sum(1 for ch in stripped if not ch.isspace())
        if visible >= 200 and cjk / max(visible, 1) < 0.15:
            return False
    except Exception:
        pass
    return True


def _persist_tool_event(workdir: Path, ev: dict) -> None:
    """PostToolUse 同时落 .tool-events.jsonl (forensics, 用户可事后查看).

    注意: runner / guard 不再读这个文件, 只是写. 控制流全部走 in-memory state.
    """
    try:
        path = workdir / _TOOL_EVENTS_FILE
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(ev, ensure_ascii=False) + "\n")
    except Exception:
        pass


# ───── SDK options 构造 ───────────────────────────────────────

def _resolve_mcp_config(workdir: Path, task_id: str | None = None) -> str | None:
    """优先用任务级 .mcp.json (含 playwright 隔离 profile)."""
    try:
        return str(build_task_mcp_config(workdir, task_id=task_id))
    except Exception:
        from pathlib import Path as _P
        for p in (_P(__file__).resolve().parent.parent / ".mcp.json",
                  _P.home() / ".claude" / ".mcp.json"):
            if p.exists():
                return str(p)
    return None


def _load_mcp_servers(workdir: Path, task_id: str) -> dict | None:
    """读 mcp_config 生成的 JSON 文件, 转成 SDK options.mcp_servers 期望的 dict.

    SDK 接受 dict 形式: {server_name: {command, args, env, type, ...}}
    """
    cfg_path = _resolve_mcp_config(workdir, task_id=task_id)
    if not cfg_path:
        return None
    try:
        cfg = json.loads(Path(cfg_path).read_text(encoding="utf-8"))
        servers = cfg.get("mcpServers") or {}
        return servers if servers else None
    except Exception:
        return None


def _make_hooks(state: GuardState, on_event: EventCB | None) -> dict:
    """SDK hook 三件套, 直接调 guard_state 纯函数 (不再走子进程协议).

    返回 SDK ClaudeAgentOptions.hooks 期望的格式.
    """
    workdir = state.workdir
    task_id = state.task_id

    async def post_tool(input_data: dict, tool_use_id, ctx):
        """PostToolUse: 累计 state + forensics 落盘."""
        tool_name = input_data.get("tool_name") or ""
        tool_input = input_data.get("tool_input") or {}
        tool_response = input_data.get("tool_response")
        observe_tool(state, tool_name, tool_input, tool_response)
        ev = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "session_id": input_data.get("session_id"),
            "cwd": input_data.get("cwd"),
            "tool_name": tool_name,
            "tool_input": tool_input,
            "tool_response": tool_response,
            "tool_use_id": tool_use_id,
            "hook_event_name": "PostToolUse",
        }
        _persist_tool_event(workdir, ev)
        return {}

    async def pre_tool(input_data: dict, tool_use_id, ctx):
        """PreToolUse: TodoWrite → Layer 1, Skill → Layer 3 + 4."""
        tool_name = input_data.get("tool_name") or ""
        payload = {
            "hook_event_name": "PreToolUse",
            "tool_name": tool_name,
            "tool_input": input_data.get("tool_input") or {},
            "cwd": input_data.get("cwd"),
            "session_id": input_data.get("session_id"),
        }
        if tool_name == "TodoWrite":
            return decision_to_sdk_pretool(check_pretool_todowrite(payload, state))
        if tool_name == "Skill":
            return decision_to_sdk_pretool(check_pretool_skill(payload, state))
        return {}

    async def stop_check(input_data: dict, tool_use_id, ctx):
        """Stop: Layer 2 + 5 + 6 + report-file 硬校验."""
        payload = {
            "hook_event_name": "Stop",
            "cwd": input_data.get("cwd"),
            "session_id": input_data.get("session_id"),
            "stop_hook_active": input_data.get("stop_hook_active", False),
        }
        return decision_to_sdk_stop(check_stop(payload, state))

    return {
        "PostToolUse": [HookMatcher(hooks=[post_tool])],
        "PreToolUse": [HookMatcher(hooks=[pre_tool])],
        "Stop": [HookMatcher(hooks=[stop_check])],
    }


def _build_sdk_options(state: GuardState, task: dict, resume: bool) -> ClaudeAgentOptions:
    """构造 SDK ClaudeAgentOptions. session_id / resume / hooks 全部注入."""
    import os
    mcp_servers = _load_mcp_servers(state.workdir, state.task_id)
    sdk_opts: dict[str, Any] = {
        "system_prompt": {
            "type": "preset",
            "preset": "claude_code",
            "append": system_append(),
        },
        "permission_mode": "bypassPermissions",
        "allowed_tools": list(_ALLOWED_TOOLS),
        "cwd": str(state.workdir),
        "hooks": _make_hooks(state, None),
        # setting_sources=["user"] 让 SDK 加载 ~/.claude/skills/ 下的用户自定义 skill
        # (hack/recon/idor/sqli/...). 不加 "project" 避免拉到任务 cwd 下的 .claude/
        # 设置 (任务目录是空的, 也不应受被测目标的配置污染).
        #
        # 历史 bug (2026-06-04 五份 EXEMPT-FULL 报告 + Read-bypass 退化):
        #   原值 setting_sources=[] 注释写"避免用户 hooks/permissions 影响",
        #   但官方文档明确: setting_sources 排除 "user" 时 ~/.claude/skills/
        #   全部不加载, AI 调 Skill(skill="hack") 真的返回 Unknown skill.
        #   AI 没说谎, 是 SDK 注册表里真没有 hack/recon 等 skill.
        #
        # 现状: dashboard 自己通过 hooks 参数注入完整 PreToolUse/Stop/PostToolUse,
        # 覆盖用户全局 hook; permission_mode="bypassPermissions" 绕过 permission.
        # 所以加 "user" source 只带进 skills, 不会与 dashboard hook 冲突.
        # 参考: https://code.claude.com/docs/en/agent-sdk/skills 的 Troubleshooting 段.
        "setting_sources": ["user"],
    }
    # session_id / resume 互斥
    if resume:
        sdk_opts["resume"] = task["session_id"]
    else:
        sdk_opts["session_id"] = task["session_id"]
    if mcp_servers:
        sdk_opts["mcp_servers"] = mcp_servers
        sdk_opts["strict_mcp_config"] = True
    # 模型选择优先级: app_settings.current_model > env ANTHROPIC_MODEL > 空 (SDK 默认)
    # 这样 UI 顶部下拉切换即时生效, 同时保留 .env 兜底.
    model = ""
    try:
        from .routers.settings import get_current_model
        model = get_current_model()
    except Exception:
        model = os.environ.get("ANTHROPIC_MODEL", "")
    if model:
        sdk_opts["model"] = model

    return ClaudeAgentOptions(
        **{k: v for k, v in sdk_opts.items() if v is not None}
    )


# ───── pump_messages: 消费 SDK message 流 ──────────────────────

@dataclass
class _TurnResult:
    """单次 query 结束 (ResultMessage 到达) 的轮次摘要."""
    needs_input_question: str | None = None
    last_stop_reason: str | None = None
    num_turns: int = 0


async def _pump_messages(
    client: ClaudeSDKClient,
    state: GuardState,
    on_event: EventCB | None,
) -> _TurnResult:
    """消费 client.receive_response() 直到 ResultMessage, 期间 emit 给 WebSocket.

    注意: 这里**不阻塞消息流** (会让 SDK 内部 buffer 堆积). pause 在 run_task 主循环
    中的 turn 边界处理 — pause 触发时 scheduler 调 client.interrupt(), 当前 turn 会以
    ResultMessage(stop_reason='interrupted') 自然结束, 本函数返回. 然后主循环
    检测 state.paused 才 await pause_event 等 unpause.
    """
    result = _TurnResult()
    task_id = state.task_id

    async for msg in client.receive_response():
        if isinstance(msg, SystemMessage):
            await _emit(
                task_id, "system",
                {"event": "init",
                 "data": {"subtype": msg.subtype, **(msg.data or {})}},
                on_event,
            )
            continue

        if isinstance(msg, AssistantMessage):
            for blk in msg.content:
                if isinstance(blk, TextBlock):
                    await _emit(task_id, "claude_text", blk.text, on_event)
                    if blk.text.startswith(NEEDS_INPUT_PREFIX):
                        q = blk.text[len(NEEDS_INPUT_PREFIX):].strip().splitlines()[0]
                        result.needs_input_question = q[:500]
                elif isinstance(blk, ThinkingBlock):
                    await _emit(
                        task_id, "thinking",
                        _truncate(blk.thinking, 2000), on_event,
                    )
                elif isinstance(blk, ToolUseBlock):
                    try:
                        inp_s = json.dumps(blk.input, ensure_ascii=False)
                    except Exception:
                        inp_s = str(blk.input)
                    await _emit(
                        task_id, "tool_use",
                        {"name": blk.name, "id": getattr(blk, "id", None),
                         "input": _truncate(inp_s, 2000)},
                        on_event,
                    )
            continue

        if isinstance(msg, UserMessage):
            if isinstance(msg.content, list):
                for blk in msg.content:
                    if not isinstance(blk, dict):
                        continue
                    if blk.get("type") == "tool_result":
                        c = blk.get("content", "")
                        if isinstance(c, list):
                            parts = []
                            for x in c:
                                if isinstance(x, dict) and x.get("type") == "text":
                                    parts.append(x.get("text", ""))
                            c = "\n".join(parts)
                        if not isinstance(c, str):
                            c = json.dumps(c, ensure_ascii=False)
                        await _emit(
                            task_id, "tool_result",
                            {"tool_use_id": blk.get("tool_use_id"),
                             "is_error": bool(blk.get("is_error")),
                             "content": _truncate(c, 4000)},
                            on_event,
                        )
            continue

        if isinstance(msg, ResultMessage):
            # 落库统计
            try:
                tokens = 0
                usage = getattr(msg, "usage", None) or {}
                if isinstance(usage, dict):
                    tokens = int(
                        (usage.get("input_tokens") or 0)
                        + (usage.get("output_tokens") or 0)
                        + (usage.get("cache_read_input_tokens") or 0)
                        + (usage.get("cache_creation_input_tokens") or 0)
                    )
                state.metrics.observe_result(
                    num_turns=msg.num_turns,
                    stop_reason=msg.stop_reason,
                    cost_usd=msg.total_cost_usd,
                    tokens=tokens,
                    duration_ms=msg.duration_ms,
                )
            except Exception:
                pass
            await _emit(
                task_id, "result",
                {"subtype": msg.subtype,
                 "is_error": msg.is_error,
                 "duration_ms": msg.duration_ms,
                 "duration_api_ms": getattr(msg, "duration_api_ms", None),
                 "num_turns": msg.num_turns,
                 "session_id": msg.session_id,
                 "stop_reason": msg.stop_reason,
                 "total_cost_usd": msg.total_cost_usd,
                 "result": msg.result},
                on_event,
            )
            result.num_turns = msg.num_turns or 0
            result.last_stop_reason = msg.stop_reason
            return result

    return result


# ───── case_a/b/c/d 早停判定与 nudge ──────────────────────────

@dataclass
class _Outcome:
    kind: str               # 'done' / 'needs_input' / 'auto_resume_a' / '_b' / '_c' / '_d' / 'max_attempts'
    nudge: str = ""
    # contract case_c 用
    contract_total: int = 0
    contract_covered: int = 0
    contract_missing_ids: list[int] | None = None
    contract_missing_ratio: float = 0.0


def _evaluate_outcome(
    state: GuardState,
    workdir: Path,
    needs_input_question: str | None,
    attempt: int,
    max_attempts: int,
) -> _Outcome:
    """决定下一步: done / needs_input / 续跑哪个 case.

    优先级: needs_input > case_d phase_skip > case_c contract > case_b browser > case_a early.
    """
    if needs_input_question:
        return _Outcome(kind="needs_input")

    report = _scan_report(workdir)

    # case_a: 真早停 (没 report + num_turns/tool_calls 极少)
    case_a = (
        report is None
        and state.metrics.is_early_stop()
    )

    # case_b: 浏览器探索深度不达标 (有真测试但 SPA 探索不达标)
    case_b = state.metrics.is_browser_shallow()

    # case_c: 契约执行不达标 (有 report 但缺漏 > 30%)
    contract_total = 0
    contract_covered = 0
    contract_missing_ids: list[int] = []
    contract_missing_ratio = 0.0
    if state.skill_name and report:
        try:
            text = (workdir / report).read_text(encoding="utf-8", errors="ignore")
            st = parse_report_contract_status(text, state.skill_name)
            contract_total = st.get("total", 0)
            covered = st.get("covered_ids", set())
            contract_covered = len(covered)
            missing = sorted(st.get("missing_ids", set()))
            contract_missing_ids = missing
            if contract_total:
                contract_missing_ratio = len(missing) / contract_total
        except Exception:
            pass
    case_c = (
        report is not None
        and contract_total > 0
        and contract_missing_ratio > 0.3
    )

    # case_d: phase 审计违规
    case_d = False
    phase_rep = None
    if state.skill_name:
        try:
            phase_rep = audit_phases(state)
            case_d = phase_rep.has_blocking_violation()
        except Exception:
            pass

    # 没有任何 case 触发, 也没 report 时 → 真早停 case_a 兜底
    if not (case_a or case_b or case_c or case_d):
        if report is None:
            # 没 report, 也没 case 触发 — 视为 case_a 早停, 但要看 attempt
            case_a = True
        else:
            return _Outcome(kind="done")

    # 已达 max_attempts, 不再续, 直接 done
    if attempt >= max_attempts:
        return _Outcome(
            kind="max_attempts",
            contract_total=contract_total,
            contract_covered=contract_covered,
            contract_missing_ids=contract_missing_ids,
            contract_missing_ratio=contract_missing_ratio,
        )

    # 按优先级生成 nudge (高优先级覆盖低优先级)
    if case_d and phase_rep is not None:
        nudge = build_phase_skip_nudge(phase_rep, attempt + 1, max_attempts)
        return _Outcome(kind="auto_resume_d", nudge=nudge)

    if case_c:
        items_full = []
        try:
            items_full = extract_contract(state.skill_name)
        except Exception:
            pass
        missing_lines: list[str] = []
        for i in contract_missing_ids[:12]:
            if 1 <= i <= len(items_full):
                missing_lines.append(f"  C{i}. {items_full[i-1][:100]}")
        tail = ""
        if len(contract_missing_ids) > 12:
            tail = f"\n  ... 还有 {len(contract_missing_ids)-12} 项未声明"
        nudge = (
            f"[契约执行不达标 / 第 {attempt+1}/{max_attempts} 次提醒]\n"
            f"skill={state.skill_name}, 契约共 {contract_total} 项, "
            f"已显式声明 {contract_covered} 项, 缺漏 {len(contract_missing_ids)} 项 "
            f"(缺漏率 {contract_missing_ratio:.0%})。\n"
            "缺漏意味着对该项你既没给 [done] 证据, 也没给 [N/A: 理由]; "
            "顶级猎人不会静默跳过任何一条契约 — 没做就明说为什么没做。\n"
            "请回到 report.md 末尾的 \"## 契约执行清单\" 一节, "
            "对下列缺漏项逐条补 [done] 证据指针 (例: '见漏洞 #2 PoC') 或 "
            "[N/A: 理由] / [skip: 理由]:\n"
            + "\n".join(missing_lines) + tail + "\n"
            "允许合并表达 (e.g. C5-C8: [N/A x4: 静态站, 无动态后端, 不适用])。"
        )
        return _Outcome(
            kind="auto_resume_c", nudge=nudge,
            contract_total=contract_total,
            contract_covered=contract_covered,
            contract_missing_ids=contract_missing_ids,
            contract_missing_ratio=contract_missing_ratio,
        )

    if case_b:
        m = state.metrics
        nudge = (
            f"[第 {attempt+1}/{max_attempts} 次提醒] 本轮浏览器探索深度不达标 — "
            "看起来你只调了一圈三件套就转去写报告了, 实际指标: "
            f"路由数={len(m.unique_routes)} (要求 ≥3), "
            f"交互动作={m.interaction_calls} (要求 ≥5), "
            f"network 重抓次数={m.network_req_calls} (要求 ≥3), "
            f"mcp 总调用={m.mcp_calls}, Bash={m.bash_calls}。\n"
            "\n"
            "SPA 的攻击面 90% 在交互之后才出现 (lazy-load chunk / 后台路由 / "
            "滚动触发的分页 / hover 才显示的管理操作 / debounce 输入触发的搜索 API), "
            "光开局调一遍 navigate+snapshot+evaluate 看到的是只有 10% 的初始静态面。\n"
            "\n"
            "请回到浏览器继续下面这些动作, 直到达标后再补充/重写 report.md:\n"
            "  - 用 browser_snapshot 看到的每个可点击导航 / tab / menu 都用 "
            "browser_click 走一遍, 每次点击后**重新调** browser_network_requests "
            "捕获新加载的 chunk 与 API\n"
            "  - 表单填出来 (browser_fill_form) 再 submit, 看看产生什么请求\n"
            "  - 用 browser_evaluate 滚动列表到底 (window.scrollTo), 看是否触发 "
            "分页 / cursor / IntersectionObserver 类的请求\n"
            "  - 主动 navigate 到常见后台路径 (/admin /dashboard /settings /users "
            "/orders /internal …) 即使首页没有链接\n"
            "  - 对每个观察到的不同 API 端点至少尝试一次: 去掉 token / 改 ID / 换方法 / "
            "换用户 token 重放, 看是否水平/垂直越权\n"
            "  - 每个发现都必须先在浏览器里复现一次再写进报告, 仅 curl 能复现的不算。\n"
        )
        return _Outcome(kind="auto_resume_b", nudge=nudge)

    # case_a
    nudge = (
        "上一轮在没有产出 report.md 的情况下提前结束 "
        f"(num_turns={state.metrics.num_turns}, "
        f"stop_reason={state.metrics.last_stop_reason})。"
        "请立即继续推进当前 prompt 中的 slash 流水线 "
        "(侦察 → 浏览器交互探索 → 枚举 → 漏洞探测 → 利用 → 报告), "
        "**默认用 playwright MCP 浏览器**, Bash 仅在静态资产 / 工具链 / 协议级"
        "测试场景里使用。最后把最终报告写入当前目录的 report.md。"
        "禁止再用单句\"明白/收到/OK\"结束本轮。"
        "若确实需要凭据 / 范围澄清, 输出一行 "
        f"`{NEEDS_INPUT_PREFIX} <问题>` 后再停。"
    )
    return _Outcome(kind="auto_resume_a", nudge=nudge)


# ───── 主入口 ──────────────────────────────────────────────────

async def run_task(
    task_id: str,
    *,
    claude_bin: str = "",  # 兼容签名 (executor 注入), SDK 不用
    timeout: int = 0,
    resume: bool = False,
    extra_input: str | None = None,
    on_event: EventCB | None = None,
) -> None:
    """SDK 版主入口. 显式循环代替递归 (case_a/b/c/d 续跑).

    流程:
    1. 取 task, 初始化 GuardState (持工具事件 + metrics + 软警告字段)
    2. 构造 ClaudeSDKClient (system_prompt / hooks / mcp / session_id)
    3. 循环 query → pump → 评估 outcome → 决定 break 或继续
    4. finalize: 算最终 status, 落 DB, emit 紫标, 发邮件
    """
    task = store.get_task(task_id)
    if not task:
        return

    workdir = Path(task["workdir"])
    workdir.mkdir(parents=True, exist_ok=True)

    # 从 task["prompt"] 检测 slash command (resume 时 extra_input 是 nudge 不含 slash)
    try:
        skill_name = detect_slash_skill(task["prompt"])
    except Exception:
        skill_name = None

    state = GuardState(task_id=task_id, workdir=workdir, skill_name=skill_name)
    pause_event = asyncio.Event()
    pause_event.set()  # 默认放行

    # DB: running
    await store.update_task(
        task_id, status="running",
        started_at=time.time(), pending_question=None,
    )
    if on_event:
        await on_event(task_id, "status", "running")

    # 首轮 prompt: 前缀强制 Skill 调用 + 原始 prompt + 契约后缀
    if resume and extra_input:
        first_prompt = extra_input
        contract_attached = False
    else:
        invocation_prefix = ""
        contract_suffix = ""
        try:
            invocation_prefix = (
                build_skill_invocation_prefix(skill_name) if skill_name else ""
            )
            contract_suffix = build_contract_prompt(skill_name) if skill_name else ""
        except Exception:
            invocation_prefix = ""
            contract_suffix = ""
        first_prompt = invocation_prefix + task["prompt"] + contract_suffix
        contract_attached = bool(contract_suffix)

    if contract_attached:
        await _emit(
            task_id, "system",
            {"event": "skill_contract_attached",
             "skill": skill_name,
             "prefix_chars": len(invocation_prefix) if not (resume and extra_input) else 0,
             "contract_chars": len(first_prompt) - len(task["prompt"])
                              - (len(invocation_prefix) if not (resume and extra_input) else 0)},
            on_event,
        )

    options = _build_sdk_options(state, task, resume=resume)
    await _emit(task_id, "system", {"event": "spawn_sdk",
                                    "session_id": task["session_id"],
                                    "resume": resume}, on_event)

    needs_input_question: str | None = None
    outcome_kind = "done"

    try:
        async with ClaudeSDKClient(options=options) as client:
            # 注册到活跃表 (供 followup / pause 复用)
            _ACTIVE_CLIENTS[task_id] = {
                "client": client,
                "state": state,
                "pause_event": pause_event,
            }

            prompt = first_prompt
            attempt = 0
            while attempt <= _MAX_RESUME_ATTEMPTS:
                if timeout and timeout > 0:
                    try:
                        await asyncio.wait_for(client.query(prompt), timeout=timeout)
                        turn = await asyncio.wait_for(
                            _pump_messages(client, state, on_event),
                            timeout=timeout,
                        )
                    except asyncio.TimeoutError:
                        await _emit(task_id, "system", {"event": "timeout"}, on_event)
                        outcome_kind = "timeout"
                        break
                else:
                    await client.query(prompt)
                    turn = await _pump_messages(client, state, on_event)

                needs_input_question = turn.needs_input_question

                # ── turn 边界: 处理 pause ──────────────────────
                # 如果用户在本 turn 期间触发了 pause, set_paused 已经调过
                # client.interrupt(), 当前 turn 以 stop_reason='interrupted' 自然结束.
                # 现在阻塞等 unpause, 期间用户可以手动登录浏览器.
                if state.paused:
                    await _emit(
                        task_id, "system",
                        {"event": "pause_blocked",
                         "msg": "已暂停, 等待用户操作浏览器后 unpause"},
                        on_event,
                    )
                    await pause_event.wait()
                    await _emit(
                        task_id, "system",
                        {"event": "pause_released",
                         "msg": "已恢复, 用 query('继续上一步未完成的工作') 重启 turn"},
                        on_event,
                    )
                    # unpause 后: 让模型从中断处继续 (新 turn). 浏览器登录态已在 .pw-profile
                    prompt = (
                        "上一轮被用户暂停, 你应当已经在浏览器里完成了手动登录. "
                        "现在请继续未完成的工作 — 不要重启浏览器, 也不要清空之前的发现, "
                        "接着原 prompt 的下一步动作走 (用 browser_navigate 当前路径 / "
                        "browser_snapshot 看登录后页面 / 继续 SPA 探索)."
                    )
                    attempt += 0  # pause 不消耗 retry 名额
                    continue

                outcome = _evaluate_outcome(
                    state, workdir, needs_input_question,
                    attempt, _MAX_RESUME_ATTEMPTS,
                )
                outcome_kind = outcome.kind

                if outcome.kind in ("done", "needs_input", "max_attempts", "timeout"):
                    break

                # auto_resume_*: emit + 准备下一轮 nudge
                reason_map = {
                    "auto_resume_a": "early_end_turn",
                    "auto_resume_b": "browser_missing",
                    "auto_resume_c": "contract_missing",
                    "auto_resume_d": "phase_skip",
                }
                await _emit(
                    task_id, "system",
                    {
                        "event": "auto_resume",
                        "reason": reason_map.get(outcome.kind, outcome.kind),
                        "attempt": attempt + 1,
                        "max_attempts": _MAX_RESUME_ATTEMPTS,
                        "num_turns": state.metrics.num_turns,
                        "stop_reason": state.metrics.last_stop_reason,
                        "mcp_calls": state.metrics.mcp_calls,
                        "bash_calls": state.metrics.bash_calls,
                        "py_web_calls": state.metrics.py_web_calls,
                        "nav_calls": state.metrics.nav_calls,
                        "unique_routes": len(state.metrics.unique_routes),
                        "interaction_calls": state.metrics.interaction_calls,
                        "network_req_calls": state.metrics.network_req_calls,
                    },
                    on_event,
                )
                prompt = outcome.nudge
                attempt += 1

    except Exception as e:
        await _emit(task_id, "system",
                    {"event": "sdk_error", "error": repr(e)}, on_event)
        await store.update_task(task_id, status="failed", finished_at=time.time(), pid=None)
        if on_event:
            await on_event(task_id, "status", "failed")
        _ACTIVE_CLIENTS.pop(task_id, None)
        return

    # 清理活跃表
    _ACTIVE_CLIENTS.pop(task_id, None)

    # ───── finalize ────────────────────────────────────────────
    await _finalize(state, workdir, needs_input_question, outcome_kind, on_event)


async def _finalize(
    state: GuardState,
    workdir: Path,
    needs_input_question: str | None,
    outcome_kind: str,
    on_event: EventCB | None,
) -> None:
    """收尾: 算 status / 紫标 / 邮件通知 / DB 落地."""
    task_id = state.task_id
    report = _scan_report(workdir)
    has_finding = 1 if report else 0

    # 契约执行状态 (供 DB + degraded)
    contract_total = 0
    contract_covered = 0
    contract_missing_ids: list[int] = []
    if state.skill_name and report:
        try:
            text = (workdir / report).read_text(encoding="utf-8", errors="ignore")
            st = parse_report_contract_status(text, state.skill_name)
            contract_total = st.get("total", 0)
            contract_covered = len(st.get("covered_ids", set()))
            contract_missing_ids = sorted(st.get("missing_ids", set()))
        except Exception:
            pass

    # 最终 status
    if needs_input_question:
        new_status = "needs_input"
    elif outcome_kind == "timeout":
        new_status = "failed"
    else:
        new_status = "done"

    # degraded 紫标
    degraded = state.metrics.is_degraded()

    # 中文报告检测
    if report and not _report_is_chinese(workdir, report):
        degraded = True
        await _emit(
            task_id, "system",
            {"event": "report_lang_warning",
             "reason": "report.md 不像中文 (CJK 占比 < 15%), skill 要求中文报告",
             "report_path": report},
            on_event,
        )

    # 契约缺漏 > 30% 也算 degraded
    if contract_total > 0:
        miss_ratio = len(contract_missing_ids) / contract_total
        if miss_ratio > 0.3:
            degraded = True
            await _emit(
                task_id, "system",
                {"event": "contract_status",
                 "skill": state.skill_name,
                 "covered": contract_covered,
                 "total": contract_total,
                 "missing_ids": list(contract_missing_ids),
                 "missing_ratio": round(miss_ratio, 2)},
                on_event,
            )

    # Stop hook bypass 紫标
    if state.stop_bypassed:
        degraded = True
        await _emit(
            task_id, "system",
            {"event": "stop_hook_bypassed", "msg": state.stop_bypassed[:200]},
            on_event,
        )

    # Layer 4 fuzz 浅紫标
    if state.fuzz_shallow:
        degraded = True
        await _emit(
            task_id, "system",
            {"event": "fuzz_shallow", "msg": state.fuzz_shallow[:300]},
            on_event,
        )

    # 软警告 skill 缺失紫标
    if state.soft_skipped:
        degraded = True
        skipped = ",".join(state.soft_skipped)
        await _emit(
            task_id, "system",
            {"event": "skill_skipped_soft",
             "skipped": skipped,
             "msg": f"hack 模式可选 skill 未调起: {skipped} — 检查目标是否需要补测"},
            on_event,
        )

    # Layer 6 retrospective 浅紫标
    if state.retro_shallow:
        degraded = True
        await _emit(
            task_id, "system",
            {"event": "retrospective_shallow", "msg": state.retro_shallow[:300]},
            on_event,
        )

    # 落 DB
    db_cols = state.metrics.to_db_columns()
    await store.update_task(
        task_id,
        status=new_status,
        finished_at=time.time(),
        exit_code=0,
        pending_question=needs_input_question,
        report_path=report,
        has_finding=has_finding,
        pid=None,
        degraded_to_python=1 if degraded else 0,
        contract_skill=state.skill_name or "",
        contract_total=contract_total,
        contract_covered=contract_covered,
        contract_missing_json=json.dumps(contract_missing_ids, ensure_ascii=False),
        **db_cols,
    )
    if degraded:
        await _emit(
            task_id, "system",
            {"event": "degraded",
             "reason": "playwright MCP 使用不足或 SPA 探索浅",
             "py_web_calls": state.metrics.py_web_calls,
             "mcp_calls": state.metrics.mcp_calls},
            on_event,
        )
    if on_event:
        await on_event(task_id, "status", new_status)

    # 邮件通知 (仅 done + has_finding)
    if new_status == "done" and has_finding:
        try:
            from .notify import send_finding_notification
            full = store.get_task(task_id) or {}
            if full.get("project_id"):
                proj = store.get_project(full["project_id"])
                if proj:
                    full["project_name"] = proj.get("name")
            stats = store.stats()
            await send_finding_notification(full, stats)
        except Exception as e:
            await store.add_event(
                task_id, "system",
                {"event": "mail_error", "error": repr(e)},
            )


# ───── stop / pause 接口 (scheduler 调) ──────────────────────────

async def stop_task(task_id: str) -> bool:
    """让 SDK client 中断当前 turn + 断开. 不动其他任务."""
    rec = _ACTIVE_CLIENTS.get(task_id)
    if not rec:
        return False
    client = rec.get("client")
    if not client:
        return False
    try:
        # SDK 提供 disconnect; interrupt 中断当前 turn
        await client.disconnect()
    except Exception:
        pass
    _ACTIVE_CLIENTS.pop(task_id, None)
    return True


async def inject_followup(task_id: str, text: str) -> bool:
    """在活跃 SDK client 上发新一轮 query (followup / continue_with).

    成功 = client 仍活, query 已发出.
    失败 = 没有活跃 client (任务终态, 调用方应回退到 retry resume 路径).
    """
    rec = _ACTIVE_CLIENTS.get(task_id)
    if not rec:
        return False
    client = rec.get("client")
    if not client:
        return False
    try:
        await client.query(text)
        return True
    except Exception:
        return False
