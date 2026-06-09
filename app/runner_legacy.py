"""Claude 任务执行器。

启动方式: claude -p --output-format stream-json --verbose --session-id <uuid>
                  --permission-mode bypassPermissions <prompt>

每个任务在独立 workdir 下运行, 工具产物 (报告/截图) 留在 workdir 内。
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import signal
from pathlib import Path
from typing import Awaitable, Callable

from . import store
from .system_prompt import NEEDS_INPUT_PREFIX, system_append

# 历史: SYSTEM_APPEND 字符串本来在此处硬编码 (~190 行).
# 现在收敛到 app/system_prompt.py 作单一来源, 此处仅做兼容性导出.
SYSTEM_APPEND = system_append()



_PROJECT_ROOT = Path(__file__).resolve().parent.parent

# 显式允许的工具集合: 内置常用工具 + 全部 playwright/jadx MCP
_ALLOWED_TOOLS = [
    "Bash", "Read", "Write", "Edit", "Grep", "Glob",
    "WebFetch", "WebSearch", "TaskCreate", "TaskUpdate", "TaskList",
    "mcp__playwright__*",
    "mcp__jadx-mcp-server__*",
]

# Bash 输入里出现以下 token, 视为 "用 python 库替代浏览器做交互" 的降级
# 注意: curl/wget/openssl/nmap 等工具调用属于正确行为 (下载静态资产/SourceMap/运行工具),
# 不纳入降级统计; 仅统计 python http 库 (requests/httpx/aiohttp/urllib) 直接访问 web 的情况
_PY_WEB_PATTERNS = (
    "requests.", "import requests", "from requests",
    "httpx.", "import httpx", "from httpx",
    "aiohttp", "urllib.request", "urllib3",
    "from playwright", "import playwright",
)

# 进程内"早停自动续跑"标记: 已经为某个 task_id 自动续过 N 次, 就不再续, 避免死循环
# 进程重启后该字典丢失, 但此时任务已不再 running, 不影响一致性
_AUTO_RESUMED: dict[str, int] = {}
# 默认每任务最多自动续一次 (温和模式); 强制阻断模式下提到 3 次
_AUTO_RESUME_MAX_DEFAULT = 1
_AUTO_RESUME_MAX_ENFORCE = 3


def _enforce_spa_depth() -> bool:
    """读环境变量决定是否进入"强制阻断"模式: SPA 探索不达标就反复续, 直到达标或上限。

    默认 off, 因为简单静态站会反复空转烧 token。给重要目标手动 export 开启:
        SECWEB_ENFORCE_SPA_DEPTH=1 ./run.sh
    """
    return os.environ.get("SECWEB_ENFORCE_SPA_DEPTH", "").strip() in ("1", "true", "yes", "on")


def _auto_resume_limit() -> int:
    return _AUTO_RESUME_MAX_ENFORCE if _enforce_spa_depth() else _AUTO_RESUME_MAX_DEFAULT

# 早停画像阈值
_EARLY_TURN_THRESHOLD = 8       # num_turns < 8 视为"刚起步就退"
_EARLY_TOOLCALL_THRESHOLD = 3   # 全部工具调用总数 < 3 (含 playwright / Bash / 任意工具)

# stdout 静默 watchdog 阈值: 如果 claude stdout 这么久没新行 (任何工具帧都算),
# 视为子进程卡死 (常见: Bash curl 卡在不退出的连接 / playwright-mcp 内部死锁 /
# macOS App Nap 把整树挂起的极端情况), 强制 SIGTERM 整组让任务进入 stopped。
# claude 自己的 Bash 工具默认 timeout 2min, 但 App Nap 时连 claude 协程时钟都被冻住,
# 那个 timeout 不会触发, 所以必须由 dashboard 这边的墙钟兜底。
_STDOUT_SILENCE_SECS = 1200     # 20 分钟无任何 stdout 输出 = 卡死

# 浏览器漏测阈值: 跑完了, 但 playwright MCP 调用偏少 + Bash 调用偏多 → 大概率被
# 诱导成"全 curl"路线, 漏掉 IDOR/XSS/CSRF/业务逻辑/SPA 等只能在真实浏览器里看到
# 的漏洞类目。命中后会自动注入一条 nudge 让模型回头补浏览器三件套基线 + 复测。
_MIN_BROWSER_CALLS = 5            # 整轮跑下来 mcp__playwright__* 工具调用数下限
_BROWSER_NUDGE_BASH_FLOOR = 6     # Bash 调用数 ≥ 这个才认为"真在跑测试", 避免空任务误触

# SPA 探索深度阈值: 仅"调过三件套"不算合格 — SPA 真正攻击面要靠交互打开。
# 完成下限 (任一不达标都自动续一轮 nudge):
_MIN_NAV_CALLS = 3            # browser_navigate ≥ 3 次 (开局 + 至少跳过 2 个路由)
_MIN_UNIQUE_ROUTES = 3        # 不同的 origin+path ≥ 3 个 (避免反复 navigate 同一页)
_MIN_INTERACTION_CALLS = 5    # click/type/fill_form/select/hover 总和 ≥ 5
_MIN_NETWORK_REQ_CALLS = 3    # browser_network_requests 至少抓 3 次 (开局 + 交互后再抓 ≥ 2)


# 长生命周期模式开关 (per task_id):
#   True  = result 到达后 **不要** 关 stdin, 让 claude 等下一轮 user message
#           (用于 pause / followup 场景, 避免重启 claude 让 playwright-mcp 死)
#   False = result 到达后关 stdin, claude 自然退出, 任务进入终态 (兼容老行为)
# scheduler 在 pause / followup 入口前 set_keep_alive(tid, True), 在 stop / 终态前 False。
_KEEP_ALIVE: dict[str, bool] = {}


def set_keep_alive(task_id: str, on: bool) -> None:
    if on:
        _KEEP_ALIVE[task_id] = True
    else:
        _KEEP_ALIVE.pop(task_id, None)


def is_keep_alive(task_id: str) -> bool:
    return _KEEP_ALIVE.get(task_id, False)


# ── 长生命周期 claude 进程的 stdin 句柄注册表 ──────────────────────────────
# 用于在不重启 claude 的前提下注入新 user message (followup):
#   stream-json 输入模式下, 父进程把 {"type":"user","message":...}\n 写入 stdin,
#   claude 接着原 session / 原 MCP 子进程 (包括 playwright-mcp 的浏览器) 处理
#   下一轮; 不需要 stop+respawn, 浏览器全程不掉。
#
# key: task_id
# value: dict(proc, stdin, finish_event, last_active_ts)
_ACTIVE: dict[str, dict] = {}


def get_active_stdin(task_id: str):
    """返回 task 当前长生命周期 claude 进程的 stdin (asyncio.StreamWriter), 或 None."""
    rec = _ACTIVE.get(task_id)
    if not rec:
        return None
    proc = rec.get("proc")
    if proc is None or proc.returncode is not None:
        return None
    return rec.get("stdin")


def get_active_finish_event(task_id: str):
    """长生命周期任务的"用户主动结束"事件 (followup 路径不会触发, 只有 stop 触发)。"""
    rec = _ACTIVE.get(task_id)
    return rec.get("finish_event") if rec else None


async def inject_user_message(task_id: str, text: str) -> bool:
    """把一段文本作为新一轮 user message 写入 claude stdin。

    成功 = 进程仍活, stdin 仍开, 写入完成。
    返回 False 表示该任务没有活跃 stream-json 进程, 调用方应回退到 stop+respawn。
    """
    rec = _ACTIVE.get(task_id)
    if not rec:
        return False
    proc = rec.get("proc")
    stdin = rec.get("stdin")
    if proc is None or proc.returncode is not None or stdin is None:
        return False
    msg = {
        "type": "user",
        "message": {
            "role": "user",
            "content": [{"type": "text", "text": text}],
        },
    }
    try:
        line = (json.dumps(msg, ensure_ascii=False) + "\n").encode("utf-8")
        stdin.write(line)
        await stdin.drain()
        return True
    except (BrokenPipeError, ConnectionResetError, RuntimeError):
        return False



def _resolve_mcp_config(workdir: Path, task_id: str | None = None) -> str | None:
    """优先用任务级 .mcp.json (含 HTTP daemon url), 退化到项目级 / 用户级."""
    try:
        from .mcp_config import build_task_mcp_config
        return str(build_task_mcp_config(workdir, task_id=task_id))
    except Exception:
        for p in (_PROJECT_ROOT / ".mcp.json", Path.home() / ".claude" / ".mcp.json"):
            if p.exists():
                return str(p)
    return None


EventCB = Callable[[str, str, str | dict], Awaitable[None]]


async def _emit(task_id: str, kind: str, payload: str | dict, cb: EventCB | None) -> None:
    await store.add_event(task_id, kind, payload)
    if cb:
        await cb(task_id, kind, payload)


# ── claude hooks 辅助数据源 ─────────────────────────────────────────────
# 通过 --settings 注入 PostToolUse hook, 让 claude 每次工具调用结束后, 把这次调用
# 的 {tool_name, tool_input, tool_response, session_id} 落到 workdir/.tool-events.jsonl。
# 这是除 stream-json 之外的"协议级"数据源:
# - 比 stream-json 解析更稳 (claude 的 stream-json 输出格式如果未来改了, hooks 仍工作)
# - 兜底统计 nav/route/interaction/network 指标, 不依赖 stdout 缓冲是否完整
# - 长进程多 turn 场景下, 多次 turn 的工具调用全部追加到同一个 JSONL 文件

_TOOL_EVENTS_FILE = ".tool-events.jsonl"
_HOOK_SETTINGS_FILE = ".hooks-settings.json"


def _write_hook_settings(workdir: Path, skill_name: str | None = None) -> Path | None:
    """在 workdir 下生成 hooks settings, 注册:
    - PostToolUse: 把每次工具调用事件追加到 .tool-events.jsonl (历史指标 + phase_audit 数据源)
    - PreToolUse(TodoWrite): pretool_guard Layer 1 — phase 标 completed 必须有对应 skill 调用
    - PreToolUse(Skill): pretool_guard Layer 3 — 上一个 Skill 后必须有足够真实工具动作
    - Stop: pretool_guard Layer 2 — hack 必经 skill 缺失时拒绝退出

    skill_name 通过 env var 传给 guard 脚本 (hook 协议没法在命令行里传业务参数).
    返回 settings 文件路径供 --settings 使用; 失败返回 None (不影响主流程)。
    """
    try:
        events_file = workdir / _TOOL_EVENTS_FILE
        # guard 脚本 + 解释器都用绝对路径, 避免 cwd 漂移
        guard_py = _PROJECT_ROOT / "app" / "pretool_guard.py"
        py_bin = _PROJECT_ROOT / ".venv" / "bin" / "python"
        if not py_bin.exists():
            py_bin = Path("python3")  # 回落系统 python3
        sk_env = (skill_name or "").lower()
        # 用 env 把 skill_name 注入子进程; PostToolUse 只追加事件文件, 不跑 guard
        # (guard 是同步阻断, PostToolUse 跑没意义)
        # shell 路径加引号防御 — 即使当前 workdir 都是 hex, 未来若改命名也安全
        post_cmd = f"cat >> '{events_file}'"
        # PreToolUse / Stop 跑 guard. claude 用 sh -c 跑 command, env 前置即可
        guard_cmd = (
            f"SECWEB_SKILL_NAME='{sk_env}' SECWEB_WORKDIR='{workdir}' "
            f"'{py_bin}' '{guard_py}'"
        )
        cfg = {
            "hooks": {
                "PostToolUse": [{
                    "matcher": ".*",
                    "hooks": [{"type": "command", "command": post_cmd}],
                }],
                "PreToolUse": [
                    {
                        "matcher": "TodoWrite",
                        "hooks": [{"type": "command", "command": guard_cmd}],
                    },
                    {
                        "matcher": "Skill",
                        "hooks": [{"type": "command", "command": guard_cmd}],
                    },
                ],
                "Stop": [{
                    "hooks": [{"type": "command", "command": guard_cmd}],
                }],
            },
        }
        path = workdir / _HOOK_SETTINGS_FILE
        path.write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
        return path
    except Exception:
        return None


def _read_hook_metrics(workdir: Path) -> dict:
    """读 workdir/.tool-events.jsonl, 重算 SPA 探索深度指标。

    返回:
      mcp_calls, bash_calls, py_web_calls,
      nav_calls, network_req_calls, interaction_calls, unique_routes (set 长度)
    任何 IO/解析错误都返回空 dict, 调用方可以回落到 stream-json 解析的结果。
    """
    out = {
        "mcp_calls": 0, "bash_calls": 0, "py_web_calls": 0,
        "nav_calls": 0, "network_req_calls": 0, "interaction_calls": 0,
        "unique_routes": 0,
    }
    f = workdir / _TOOL_EVENTS_FILE
    if not f.exists():
        return {}
    routes: set[str] = set()
    interaction_names = {
        "browser_click", "browser_type", "browser_fill_form",
        "browser_select_option", "browser_hover",
        "browser_press_key", "browser_drag",
    }
    try:
        with f.open(encoding="utf-8") as fp:
            for line in fp:
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                except json.JSONDecodeError:
                    continue
                name = (d.get("tool_name") or "").strip()
                inp = d.get("tool_input") or {}
                if name.startswith("mcp__playwright__"):
                    out["mcp_calls"] += 1
                    short = name[len("mcp__playwright__"):]
                    if short == "browser_navigate":
                        out["nav_calls"] += 1
                        try:
                            u = (inp.get("url") if isinstance(inp, dict) else "") or ""
                            if u:
                                from urllib.parse import urlparse
                                pr = urlparse(u)
                                key = f"{pr.scheme}://{pr.netloc}{pr.path}".rstrip("/")
                                if key:
                                    routes.add(key)
                        except Exception:
                            pass
                    elif short == "browser_network_requests":
                        out["network_req_calls"] += 1
                    elif short in interaction_names:
                        out["interaction_calls"] += 1
                elif name == "Bash":
                    out["bash_calls"] += 1
                    try:
                        cmdtxt = json.dumps(inp, ensure_ascii=False).lower()
                    except Exception:
                        cmdtxt = ""
                    if any(k in cmdtxt for k in _PY_WEB_PATTERNS):
                        out["py_web_calls"] += 1
        out["unique_routes"] = len(routes)
        return out
    except Exception:
        return {}


def _build_cmd(
    claude_bin: str,
    session_id: str,
    resume: bool,
    workdir: Path,
    task_id: str | None = None,
    skill_name: str | None = None,
) -> list[str]:
    """构造 claude CLI 启动命令。
    使用 --input-format stream-json: 不再传位置 prompt, 改成把 user message
    通过 stdin 喂给同一个长生命周期进程 (支持多轮 turn / followup 注入)。"""
    cmd = [
        claude_bin,
        "-p",
        "--input-format", "stream-json",
        "--output-format", "stream-json",
        "--verbose",
        "--dangerously-skip-permissions",
        "--append-system-prompt", SYSTEM_APPEND,
        "--allowedTools", *_ALLOWED_TOOLS,
    ]
    mcp_cfg = _resolve_mcp_config(workdir, task_id=task_id)
    if mcp_cfg:
        cmd += ["--mcp-config", mcp_cfg, "--strict-mcp-config"]
    # PostToolUse hook → 工具调用事件落 .tool-events.jsonl
    # PreToolUse + Stop hook → pretool_guard 三层守卫 (Skill 调用真实性 + Stop 阻断)
    hook_settings = _write_hook_settings(workdir, skill_name=skill_name)
    if hook_settings:
        cmd += ["--settings", str(hook_settings)]
    if resume:
        cmd += ["--resume", session_id]
    else:
        cmd += ["--session-id", session_id]
    return cmd


def _extract_text(msg: dict) -> str:
    """从 stream-json 的 assistant 消息中抽出纯文本 (用于 NEEDS_USER_INPUT 检测)"""
    out = []
    try:
        content = msg.get("message", {}).get("content", [])
        if isinstance(content, str):
            return content
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                out.append(block.get("text", ""))
    except Exception:
        pass
    return "\n".join(s for s in out if s)


def _truncate(s: str, n: int = 4000) -> str:
    if len(s) <= n:
        return s
    return s[:n] + f"\n…[truncated {len(s)-n} chars]"


async def _emit_assistant_blocks(
    task_id: str, msg: dict, on_event: EventCB | None
) -> str:
    """把 assistant 消息按 content block 拆开发事件, 返回拼接的纯文本(供检测标记)"""
    full_text: list[str] = []
    try:
        content = msg.get("message", {}).get("content", [])
        if isinstance(content, str):
            await _emit(task_id, "claude_text", content, on_event)
            return content
        for block in content:
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if btype == "text":
                txt = block.get("text", "")
                if txt:
                    full_text.append(txt)
                    await _emit(task_id, "claude_text", txt, on_event)
            elif btype == "tool_use":
                inp = block.get("input", {})
                try:
                    inp_s = json.dumps(inp, ensure_ascii=False)
                except Exception:
                    inp_s = str(inp)
                await _emit(
                    task_id, "tool_use",
                    {
                        "name": block.get("name", "?"),
                        "id": block.get("id"),
                        "input": _truncate(inp_s, 2000),
                    },
                    on_event,
                )
            elif btype == "thinking":
                t = block.get("thinking", "")
                if t:
                    await _emit(task_id, "thinking", _truncate(t, 2000), on_event)
    except Exception as e:
        await _emit(task_id, "system", {"event": "parse_error", "error": str(e)}, on_event)
    return "\n".join(full_text)


async def _emit_user_blocks(
    task_id: str, msg: dict, on_event: EventCB | None
) -> None:
    """user 消息中的 tool_result (工具执行结果回流)"""
    try:
        content = msg.get("message", {}).get("content", [])
        if not isinstance(content, list):
            return
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_result":
                c = block.get("content", "")
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
                    {
                        "tool_use_id": block.get("tool_use_id"),
                        "is_error": bool(block.get("is_error")),
                        "content": _truncate(c, 4000),
                    },
                    on_event,
                )
    except Exception:
        pass


def _scan_report(workdir: Path) -> str | None:
    """挑出最像最终报告的 markdown 文件 (相对路径)"""
    if not workdir.exists():
        return None
    candidates: list[tuple[int, Path]] = []
    for p in workdir.rglob("*.md"):
        name = p.name.lower()
        score = 0
        if name == "report.md":
            score = 100
        elif "report" in name:
            score = 50
        elif "finding" in name or "vuln" in name:
            score = 30
        else:
            score = 1
        try:
            score += min(p.stat().st_size // 200, 30)  # 越长越像正式报告
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


async def run_task(
    task_id: str,
    *,
    claude_bin: str,
    timeout: int,
    resume: bool,
    extra_input: str | None,
    on_event: EventCB | None,
) -> None:
    """执行一个任务。resume=True 表示用 extra_input 续跑现有 session。"""
    task = store.get_task(task_id)
    if not task:
        return

    workdir = Path(task["workdir"])
    workdir.mkdir(parents=True, exist_ok=True)

    prompt = extra_input if (resume and extra_input) else task["prompt"]
    # 提前检测首条 prompt 的 slash command, 让 hook settings 能注入 SECWEB_SKILL_NAME
    # resume 时 extra_input 是 nudge 文本不含 slash, 但任务初始 prompt 还在 task["prompt"]
    # 必须从 task["prompt"] 检测, 否则续跑时 skill_name=None → 守卫全失效
    try:
        from .skill_contract import detect_slash_skill
        early_skill_name = detect_slash_skill(task["prompt"])
    except Exception:
        early_skill_name = None
    cmd = _build_cmd(
        claude_bin, task["session_id"], resume=resume,
        workdir=workdir, task_id=task_id, skill_name=early_skill_name,
    )

    await _emit(task_id, "system", {"event": "spawn", "cmd": cmd}, on_event)
    await store.update_task(
        task_id,
        status="running",
        started_at=__import__("time").time(),
        pending_question=None,
    )
    if on_event:
        await on_event(task_id, "status", "running")

    env = os.environ.copy()
    # MCP 客户端冷启动 (尤其首次拉 chromium) 容易超过 claude 默认握手窗口,
    # 显式拉长到 2 分钟; 工具调用 (browser_navigate 等) 拉长到 5 分钟,
    # 避免长加载页 / 大流量抓取被打断
    env.setdefault("MCP_TIMEOUT", "120000")
    env.setdefault("MCP_TOOL_TIMEOUT", "300000")
    # 任务跑在独立 workdir, claude 的 CLAUDE.md 自发现仍生效
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=str(workdir),
        env=env,
        stdin=asyncio.subprocess.PIPE,   # stream-json 输入: 父进程写 user message
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        # 新进程组, 方便整组终止 (避免 claude 派生的子进程逃逸)
        start_new_session=True,
    )
    await store.update_task(task_id, pid=proc.pid)

    # 注册到活跃任务表, 让 followup/inject_user_message 能拿到 stdin
    finish_event = asyncio.Event()
    _ACTIVE[task_id] = {
        "proc": proc,
        "stdin": proc.stdin,
        "finish_event": finish_event,
    }

    # 发送第一轮 user message (相当于原来位置参数 prompt 的角色)
    # 在 prompt 末尾追加从 skill 文件抽出的"执行契约", 让模型对每条强制项给出 done/N/A
    try:
        from .skill_contract import build_contract_prompt
        # contract 仅在首轮注入, 续跑时不重复 (避免 prompt 撑大)
        skill_name = early_skill_name if not resume else None
        contract_suffix = build_contract_prompt(skill_name) if skill_name else ""
    except Exception:
        contract_suffix = ""
        skill_name = None
    first_prompt_text = prompt + contract_suffix
    if contract_suffix:
        await _emit(
            task_id, "system",
            {"event": "skill_contract_attached",
             "skill": skill_name,
             "contract_chars": len(contract_suffix)},
            on_event,
        )
    try:
        first_msg = {
            "type": "user",
            "message": {
                "role": "user",
                "content": [{"type": "text", "text": first_prompt_text}],
            },
        }
        proc.stdin.write((json.dumps(first_msg, ensure_ascii=False) + "\n").encode("utf-8"))
        await proc.stdin.drain()
    except (BrokenPipeError, ConnectionResetError):
        pass

    needs_input_question: str | None = None
    mcp_playwright_calls = 0
    python_web_fallback_calls = 0
    bash_tool_calls = 0          # 任意 Bash 工具调用次数 (含合理的 curl/wget/工具链)
    last_num_turns = 0
    last_stop_reason: str | None = None
    last_stdout_ts = asyncio.get_event_loop().time()  # 最近一次 stdout 行的墙钟
    silence_killed = False                            # 静默 watchdog 触发过 kill 的标记
    compacting = False                                # 1M ctx compaction 进行中 (期间 stdout 自然停顿, 不能算卡死)
    # 浏览器探索深度计量 (用于"完成下限"判定, 比单纯 mcp_calls 计数更难造假):
    nav_calls = 0                  # browser_navigate 次数
    network_req_calls = 0          # browser_network_requests 次数 (re-fetch network panel)
    interaction_calls = 0          # 交互动作: click / type / fill_form / select_option / hover / press_key / drag
    unique_routes: set[str] = set()  # 不同的 navigate 目标 URL (粗略路由覆盖度)

    async def pump_stdout() -> None:
        nonlocal needs_input_question, mcp_playwright_calls, python_web_fallback_calls
        nonlocal bash_tool_calls, last_num_turns, last_stop_reason, last_stdout_ts
        nonlocal nav_calls, network_req_calls, interaction_calls, compacting
        assert proc.stdout
        async for raw in proc.stdout:
            last_stdout_ts = asyncio.get_event_loop().time()
            line = raw.decode("utf-8", errors="replace").rstrip("\n")
            if not line:
                continue
            # 优先按 stream-json 解析
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                await _emit(task_id, "stdout", line, on_event)
                if NEEDS_INPUT_PREFIX in line:
                    needs_input_question = line.split(NEEDS_INPUT_PREFIX, 1)[1].strip()
                continue

            mtype = msg.get("type")
            if mtype == "assistant":
                text = await _emit_assistant_blocks(task_id, msg, on_event)
                # 统计工具用量: 区分 playwright MCP 与 Python web 降级
                content = msg.get("message", {}).get("content", [])
                if isinstance(content, list):
                    for block in content:
                        if not isinstance(block, dict) or block.get("type") != "tool_use":
                            continue
                        name = block.get("name", "") or ""
                        if name.startswith("mcp__playwright__"):
                            mcp_playwright_calls += 1
                            # 细分计量: navigate / network_requests / 交互类
                            short = name[len("mcp__playwright__"):]
                            if short == "browser_navigate":
                                nav_calls += 1
                                try:
                                    inp = block.get("input", {}) or {}
                                    u = (inp.get("url") if isinstance(inp, dict) else "") or ""
                                    # 粗粒度: 取 origin+path, 忽略 query/fragment, 去掉末尾斜杠
                                    if u:
                                        from urllib.parse import urlparse
                                        pr = urlparse(u)
                                        key = f"{pr.scheme}://{pr.netloc}{pr.path}".rstrip("/")
                                        if key:
                                            unique_routes.add(key)
                                except Exception:
                                    pass
                            elif short == "browser_network_requests":
                                network_req_calls += 1
                            elif short in (
                                "browser_click", "browser_type", "browser_fill_form",
                                "browser_select_option", "browser_hover",
                                "browser_press_key", "browser_drag",
                            ):
                                interaction_calls += 1
                        elif name == "Bash":
                            bash_tool_calls += 1
                            try:
                                cmdtxt = json.dumps(
                                    block.get("input", {}), ensure_ascii=False
                                ).lower()
                            except Exception:
                                cmdtxt = ""
                            if any(k in cmdtxt for k in _PY_WEB_PATTERNS):
                                python_web_fallback_calls += 1
                if text and NEEDS_INPUT_PREFIX in text:
                    for ln in text.splitlines():
                        if NEEDS_INPUT_PREFIX in ln:
                            needs_input_question = ln.split(
                                NEEDS_INPUT_PREFIX, 1
                            )[1].strip()
                            break
            elif mtype == "user":
                await _emit_user_blocks(task_id, msg, on_event)
            elif mtype == "result":
                await _emit(task_id, "result", msg, on_event)
                # 抽 cost / tokens / duration 落库, 用于预算与统计
                try:
                    usage = (msg.get("usage") or {}) if isinstance(msg, dict) else {}
                    cost = float(msg.get("total_cost_usd") or msg.get("cost_usd") or 0)
                    tokens = int(
                        (usage.get("input_tokens") or 0)
                        + (usage.get("output_tokens") or 0)
                        + (usage.get("cache_read_input_tokens") or 0)
                        + (usage.get("cache_creation_input_tokens") or 0)
                    )
                    duration = int(msg.get("duration_ms") or 0)
                    await store.update_task(
                        task_id,
                        cost_usd=cost,
                        total_tokens=tokens,
                        duration_ms=duration,
                    )
                except Exception:
                    pass
                # 记录 turn 数 / 终止原因, 给早停自动续跑做判定
                try:
                    nt = msg.get("num_turns")
                    if isinstance(nt, int):
                        last_num_turns = nt
                    sr = msg.get("stop_reason")
                    if isinstance(sr, str):
                        last_stop_reason = sr
                except Exception:
                    pass
                # 单 turn 结束: 是否就此关 stdin 让 claude 退出?
                # - keep_alive 开 (来自 pause/followup): 保持 stdin 等下一轮
                # - 否则: 关 stdin, claude 退出 → 进入终态 (兼容老 one-shot 行为)
                if not is_keep_alive(task_id):
                    try:
                        if proc.stdin and not proc.stdin.is_closing():
                            proc.stdin.close()
                    except Exception:
                        pass
            elif mtype == "system":
                await _emit(task_id, "system", {"event": "init", "data": msg}, on_event)
                # 跟踪 1M ctx compaction 状态: compacting 期间模型在做上下文压缩,
                # stdout 不会冒新工具帧, 但进程是健康的, silence_watchdog 必须跳过判定。
                # compact_boundary 标志压缩完成 → 解除 compacting 标记。
                try:
                    sub = msg.get("subtype")
                    if sub == "compacting":
                        compacting = True
                        last_stdout_ts = asyncio.get_event_loop().time()
                    elif sub == "compact_boundary":
                        compacting = False
                        last_stdout_ts = asyncio.get_event_loop().time()
                except Exception:
                    pass
            else:
                await _emit(task_id, "system", msg, on_event)

    async def pump_stderr() -> None:
        nonlocal last_stdout_ts
        assert proc.stderr
        async for raw in proc.stderr:
            last_stdout_ts = asyncio.get_event_loop().time()
            line = raw.decode("utf-8", errors="replace").rstrip("\n")
            if line:
                await _emit(task_id, "stderr", line, on_event)

    async def silence_watchdog() -> None:
        """监督 claude 子进程 stdout 输出, 长时间静默就强杀整组。

        典型卡死场景:
        - claude 内 Bash 工具调用 (e.g. curl) 永不返回, claude 协程时钟也不走
          (尤其在 macOS App Nap 把整树挂起时, claude 自己的 2min Bash timeout
          失效)
        - playwright-mcp 内部死锁 / 浏览器 hang
        - API 长连接被对端掐, claude 不重连也不退出
        阈值 _STDOUT_SILENCE_SECS, 触发后 SIGTERM 整组, 让 run_task 主循环
        感知到 proc 退出并写终态。

        例外: 任务处于 paused 状态时 (用户主动 SIGSTOP claude 等手动登录) 跳过判定,
        否则会把 playwright-mcp 浏览器一起杀掉, 用户的暂停-登录-继续工作流就废了。
        """
        nonlocal silence_killed, last_stdout_ts
        loop = asyncio.get_event_loop()
        while True:
            try:
                await asyncio.sleep(60)
            except asyncio.CancelledError:
                return
            if proc.returncode is not None:
                return
            # paused 是用户主动行为, 不算卡死, 直接跳过 + 把"上次 stdout 时间"推到现在
            # 这样恢复后用户也能享受到完整的 _STDOUT_SILENCE_SECS 容差期, 不会一恢复就被杀
            t_now = store.get_task(task_id) or {}
            if t_now.get("status") == "paused":
                last_stdout_ts = loop.time()
                continue
            # compaction 期间模型在做 1M ctx 上下文压缩, stdout 自然停顿但进程健康。
            # 跳过判定 + 重置墙钟, 否则压缩耗时一过 _STDOUT_SILENCE_SECS 就会误杀。
            if compacting:
                last_stdout_ts = loop.time()
                continue
            silent_for = loop.time() - last_stdout_ts
            if silent_for > _STDOUT_SILENCE_SECS:
                silence_killed = True
                await _emit(
                    task_id, "system",
                    {
                        "event": "silence_kill",
                        "silent_for_secs": int(silent_for),
                        "threshold_secs": _STDOUT_SILENCE_SECS,
                    },
                    on_event,
                )
                _kill_group(proc)
                return

    pumps = asyncio.gather(pump_stdout(), pump_stderr(), silence_watchdog())
    try:
        if timeout and timeout > 0:
            await asyncio.wait_for(proc.wait(), timeout=timeout)
        else:
            await proc.wait()
        await pumps
    except asyncio.TimeoutError:
        await _emit(task_id, "system", {"event": "timeout"}, on_event)
        _kill_group(proc)
        try:
            await asyncio.wait_for(proc.wait(), timeout=10)
        except asyncio.TimeoutError:
            pass
    except asyncio.CancelledError:
        await _emit(task_id, "system", {"event": "cancelled"}, on_event)
        _kill_group(proc)
        try:
            await asyncio.wait_for(proc.wait(), timeout=5)
        except asyncio.TimeoutError:
            pass
        # 清理活跃句柄, 避免后续 followup 写到死管道
        _ACTIVE.pop(task_id, None)
        set_keep_alive(task_id, False)
        # 即使被取消也把 hook 数据合并写库, 否则前端显示 0 而 JSONL 里有真实数据,
        # 等于丢失了已经发生的探索深度证据
        try:
            hook_m = _read_hook_metrics(workdir)
            if hook_m:
                # stop_task 已经写过 status=stopped + finished_at + pid=None,
                # 这里只追加指标列, 不动状态
                await store.update_task(
                    task_id,
                    mcp_calls=max(mcp_playwright_calls, hook_m.get("mcp_calls", 0)),
                    py_web_calls=max(python_web_fallback_calls, hook_m.get("py_web_calls", 0)),
                    nav_calls=max(nav_calls, hook_m.get("nav_calls", 0)),
                    unique_routes=max(len(unique_routes), hook_m.get("unique_routes", 0)),
                    interaction_calls=max(interaction_calls, hook_m.get("interaction_calls", 0)),
                    network_req_calls=max(network_req_calls, hook_m.get("network_req_calls", 0)),
                )
        except Exception:
            pass
        raise
    finally:
        try:
            await pumps
        except Exception:
            pass

    # 清理活跃句柄 (无论 done/failed/timeout, 该 proc 已不可用)
    _ACTIVE.pop(task_id, None)
    set_keep_alive(task_id, False)

    rc = proc.returncode or 0
    report = _scan_report(workdir)
    has_finding = 1 if report else 0

    # 提前计算契约执行状态 (供 case_c_contract 与最终 degraded 共用)
    contract_total = 0
    contract_covered_n = 0
    contract_missing_ids: list[int] = []
    contract_missing_ratio = 0.0
    if skill_name and report:
        try:
            from .skill_contract import parse_report_contract_status
            text_for_contract = (workdir / report).read_text(encoding="utf-8", errors="ignore")
            st = parse_report_contract_status(text_for_contract, skill_name)
            contract_total = st.get("total", 0)
            covered = st.get("covered_ids", set())
            contract_covered_n = len(covered)
            missing = sorted(st.get("missing_ids", set()))
            contract_missing_ids = missing
            if contract_total:
                contract_missing_ratio = len(missing) / contract_total
        except Exception:
            pass

    # phase 执行审计: 从 .tool-events.jsonl 重建 phase 轨迹, 检测
    # "TodoWrite 标 completed 但同名 skill 从未被 Skill 工具调起"
    # 这是 hack/SKILL.md 第一条硬规则的执行层验证, report.md 文本校验骗不过
    phase_audit_rep = None
    if skill_name:
        try:
            from .phase_audit import audit as phase_audit_run
            phase_audit_rep = phase_audit_run(
                workdir / _TOOL_EVENTS_FILE, skill_name
            )
        except Exception:
            phase_audit_rep = None

    # ─── 早停 / 浏览器漏测 / 契约违规 自动续跑兜底 ───────────────────
    # 命中以下任一情形都续一轮 (同一 session_id), 但每个任务最多自动续一次:
    #
    # A) 真早停: 模型几乎没干活就 end_turn (老逻辑)
    #    - 退出码 0 / 没有挂起问题 / 没产出 report
    #    - num_turns 很少 + 任意工具调用总数也很少
    #
    # B) 浏览器探索深度不达标: 调过 playwright 但停在"打卡式三件套"
    #    - 退出码 0 / 没有挂起问题
    #    - 跑了真测试 (total_tool_calls 够多)
    #    - 但下面任一 SPA 探索维度不达标:
    #         路由数 < _MIN_UNIQUE_ROUTES
    #      或 交互动作数 < _MIN_INTERACTION_CALLS
    #      或 重新抓 network panel 次数 < _MIN_NETWORK_REQ_CALLS
    #    SPA 攻击面 90% 在交互之后才出现, 仅靠 navigate+snapshot+evaluate 一圈
    #    打卡式调用根本看不到 lazy-loaded chunk / 后台路由 / 滚动触发的 API。
    #    命中即使产出了 report 也续跑, 让模型回去真实地点点页面 + 重抓 network。
    total_tool_calls = (
        mcp_playwright_calls + bash_tool_calls + python_web_fallback_calls
    )
    case_a_early = (
        rc == 0
        and not needs_input_question
        and report is None
        and 0 < last_num_turns < _EARLY_TURN_THRESHOLD
        and total_tool_calls < _EARLY_TOOLCALL_THRESHOLD
    )
    case_b_browser_missing = (
        rc == 0
        and not needs_input_question
        and total_tool_calls >= _EARLY_TOOLCALL_THRESHOLD  # 不是空跑
        and (
            len(unique_routes) < _MIN_UNIQUE_ROUTES
            or interaction_calls < _MIN_INTERACTION_CALLS
            or network_req_calls < _MIN_NETWORK_REQ_CALLS
        )
        # 旧维度做兜底: 哪怕路由/交互够了, 但 mcp 调用极低 + bash 暴多, 也算降级
        or (
            rc == 0
            and not needs_input_question
            and mcp_playwright_calls < _MIN_BROWSER_CALLS
            and bash_tool_calls >= _BROWSER_NUDGE_BASH_FLOOR
        )
    )
    case_c_contract = (
        rc == 0
        and not needs_input_question
        and report is not None
        and contract_total > 0
        and contract_missing_ratio > 0.3
    )
    # case_d: phase 审计违规 — TodoWrite 标 completed 但 skill 从未被 Skill 工具调起
    # 即使 report 已写出, 这种"假装执行"也必须续跑补齐, 否则报告基于盲区
    case_d_phase_skip = (
        rc == 0
        and not needs_input_question
        and phase_audit_rep is not None
        and phase_audit_rep.has_blocking_violation()
    )
    early_stop = (
        (case_a_early or case_b_browser_missing or case_c_contract or case_d_phase_skip)
        and _AUTO_RESUMED.get(task_id, 0) < _auto_resume_limit()
    )
    if early_stop:
        _AUTO_RESUMED[task_id] = _AUTO_RESUMED.get(task_id, 0) + 1
        attempt = _AUTO_RESUMED[task_id]
        reason = (
            "phase_skip" if case_d_phase_skip else
            "contract_missing" if case_c_contract else
            "browser_missing" if case_b_browser_missing else
            "early_end_turn"
        )
        await _emit(
            task_id, "system",
            {
                "event": "auto_resume",
                "reason": reason,
                "attempt": attempt,
                "max_attempts": _auto_resume_limit(),
                "enforce_mode": _enforce_spa_depth(),
                "num_turns": last_num_turns,
                "stop_reason": last_stop_reason,
                "mcp_calls": mcp_playwright_calls,
                "bash_calls": bash_tool_calls,
                "py_web_calls": python_web_fallback_calls,
                "nav_calls": nav_calls,
                "unique_routes": len(unique_routes),
                "interaction_calls": interaction_calls,
                "network_req_calls": network_req_calls,
            },
            on_event,
        )
        if case_b_browser_missing:
            enforce_hint = (
                f"[强制阻断模式 已开启 / 第 {attempt}/{_auto_resume_limit()} 次提醒] "
                if _enforce_spa_depth() else
                f"[第 {attempt}/{_auto_resume_limit()} 次提醒] "
            )
            nudge = (
                f"{enforce_hint}本轮浏览器探索深度不达标 — 看起来你只调了一圈三件套就转去写报告了, "
                f"实际指标: 路由数={len(unique_routes)} (要求 ≥{_MIN_UNIQUE_ROUTES}), "
                f"交互动作={interaction_calls} (要求 ≥{_MIN_INTERACTION_CALLS}), "
                f"network 重抓次数={network_req_calls} (要求 ≥{_MIN_NETWORK_REQ_CALLS}), "
                f"mcp 总调用={mcp_playwright_calls}, Bash={bash_tool_calls}。\n"
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
                "\n"
                "如果你认为这个目标确实没有更多可做的交互 (例如纯静态营销页 / 只有一个登录页), "
                "在 report.md 的最后写一节 \"已穷尽的交互列表\", 列出所有你尝试过的 click/"
                "navigate 路径和它们各自产出的 network panel 摘要, 而不是静默跳过。"
            )
        else:
            nudge = (
                "上一轮在没有产出 report.md 的情况下提前结束 (num_turns="
                f"{last_num_turns}, stop_reason={last_stop_reason})。"
                "请立即继续推进当前 prompt 中的 slash 流水线 "
                "(侦察 → 浏览器交互探索 → 枚举 → 漏洞探测 → 利用 → 报告), "
                "**默认用 playwright MCP 浏览器**, Bash 仅在静态资产 / 工具链 / 协议级"
                "测试场景里使用。最后把最终报告写入当前目录的 report.md。"
                "禁止再用单句\"明白/收到/OK\"结束本轮。"
                "若确实需要凭据 / 范围澄清, 输出一行 "
                f"`{NEEDS_INPUT_PREFIX} <问题>` 后再停。"
            )
        # case_c 契约缺失: 优先使用契约 nudge (信号最强, 直接告诉模型缺哪些 ID)
        if case_c_contract:
            from .skill_contract import extract_contract
            try:
                items_full = extract_contract(skill_name)
            except Exception:
                items_full = []
            missing_lines = []
            for i in contract_missing_ids[:12]:  # 限制 nudge 长度
                if 1 <= i <= len(items_full):
                    missing_lines.append(f"  C{i}. {items_full[i-1][:100]}")
            tail = ""
            if len(contract_missing_ids) > 12:
                tail = f"\n  ... 还有 {len(contract_missing_ids)-12} 项未声明"
            nudge = (
                f"[契约执行不达标 / 第 {attempt}/{_auto_resume_limit()} 次提醒]\n"
                f"skill={skill_name}, 契约共 {contract_total} 项, "
                f"已显式声明 {contract_covered_n} 项, 缺漏 {len(contract_missing_ids)} 项 "
                f"(缺漏率 {contract_missing_ratio:.0%})。\n"
                "缺漏意味着对该项你既没给 [done] 证据, 也没给 [N/A: 理由]; "
                "顶级猎人不会静默跳过任何一条契约 — 没做就明说为什么没做。\n"
                "请回到 report.md 末尾的 \"## 契约执行清单\" 一节, "
                "对下列缺漏项逐条补 [done] 证据指针 (例: '见漏洞 #2 PoC') 或 "
                "[N/A: 理由] / [skip: 理由]:\n"
                + "\n".join(missing_lines) + tail + "\n"
                "允许合并表达 (e.g. C5-C8: [N/A x4: 静态站, 无动态后端, 不适用])。"
            )
        # case_d_phase_skip 信号最强 (Skill 工具调用记录 > 报告文本声明),
        # 优先用 phase_audit nudge 覆盖前面的 case_b/case_c 文本
        if case_d_phase_skip and phase_audit_rep is not None:
            from .phase_audit import build_phase_skip_nudge
            nudge = build_phase_skip_nudge(
                phase_audit_rep, attempt, _auto_resume_limit()
            )
        # 递归续跑一次. resume=True 复用同一 session_id, 上下文完整。
        await run_task(
            task_id,
            claude_bin=claude_bin,
            timeout=timeout,
            resume=True,
            extra_input=nudge,
            on_event=on_event,
        )
        return
    # ────────────────────────────────────────────────────────────────

    if needs_input_question:
        new_status = "needs_input"
    elif rc == 0:
        new_status = "done"
    else:
        # silence_watchdog 是 liveness 安全网, 不是失败信号本身。
        # 如果 watchdog kill 时 report.md 已经写好 + 上一轮 result 自己声明是 success,
        # 应当当作 done 收尾, 而不是用 SIGTERM 的 143 把状态降成 failed。
        # 续跑场景下 (case_c_contract / case_b_browser) 尤其重要: 第一轮已经 done,
        # 续跑那一轮被 watchdog 砍不应该让用户看到"明明有报告却失败"。
        if silence_killed and report and last_stop_reason in ("end_turn", "stop_sequence", None):
            new_status = "done"
            await _emit(
                task_id, "system",
                {
                    "event": "silence_kill_recovered",
                    "reason": "watchdog killed but report exists + last result was success",
                    "exit_code": rc,
                },
                on_event,
            )
        else:
            new_status = "failed"

    import time as _t
    # 优先用 PostToolUse hook 落地的事件文件做最终指标统计:
    # hooks 是 claude 协议级钩子, 即使 stream-json 输出异常 / 行被截断, hooks 也会触发。
    # 拿到 hook 数据后, 与 stream-json 解析的计数取较大值 (兼具两个数据源, 兜底)。
    hook_m = _read_hook_metrics(workdir)
    if hook_m:
        # 取 max 以容纳: stream-json 多看到一些 (含未走 hooks 的本机工具? 不会, 但保险)
        # / hook 多看到一些 (stream-json 截断时)
        mcp_playwright_calls = max(mcp_playwright_calls, hook_m.get("mcp_calls", 0))
        bash_tool_calls      = max(bash_tool_calls,      hook_m.get("bash_calls", 0))
        python_web_fallback_calls = max(python_web_fallback_calls, hook_m.get("py_web_calls", 0))
        nav_calls            = max(nav_calls,            hook_m.get("nav_calls", 0))
        network_req_calls    = max(network_req_calls,    hook_m.get("network_req_calls", 0))
        interaction_calls    = max(interaction_calls,    hook_m.get("interaction_calls", 0))
        if hook_m.get("unique_routes", 0) > len(unique_routes):
            # hook 看到的路由数更多 — 用占位符撑数: 后面写库时 len(unique_routes) 已经反映
            # (我们把占位 url 加到 set 里以增加 size; 真实 url 已在 hook JSONL 内)
            need = hook_m["unique_routes"] - len(unique_routes)
            for i in range(need):
                unique_routes.add(f"__hook_route_{i}__")
        # 也把 case_b / early_stop 的 total_tool_calls 重算一次 (基于新计数)
        total_tool_calls = (
            mcp_playwright_calls + bash_tool_calls + python_web_fallback_calls
        )
    # degraded 四种情形都打:
    #   (a) 完全没 pw + 用了 python 库
    #   (b) 跑了真测试但 pw 调用偏少 (没满浏览器三件套基线)
    #   (c) Bash 占比明显高于 pw (>= 2x), 大概率被诱导成 curl 路线
    #   (d) SPA 探索深度不够: 路由/交互/network 重抓任一不达标 (打卡式)
    spa_depth_missing = (
        total_tool_calls >= _EARLY_TOOLCALL_THRESHOLD
        and (
            len(unique_routes) < _MIN_UNIQUE_ROUTES
            or interaction_calls < _MIN_INTERACTION_CALLS
            or network_req_calls < _MIN_NETWORK_REQ_CALLS
        )
    )
    degraded = (
        (mcp_playwright_calls == 0 and python_web_fallback_calls > 0)
        or (
            mcp_playwright_calls < _MIN_BROWSER_CALLS
            and bash_tool_calls >= _BROWSER_NUDGE_BASH_FLOOR
        )
        or (
            mcp_playwright_calls > 0
            and bash_tool_calls >= mcp_playwright_calls * 2
        )
        or spa_depth_missing
    )
    # 报告语言检测: skill 要求中文, 实际经常输出英文。扫 report.md 里 CJK 占比,
    # 太低就 emit 一条 system 事件提醒用户 + 直接当作 degraded 触发紫标
    report_non_cn = False
    if report:
        try:
            text = (workdir / report).read_text(encoding="utf-8", errors="ignore")
            # 去掉代码块再判断 (代码块里的英文不算)
            stripped = re.sub(r"```.*?```", "", text, flags=re.DOTALL)
            stripped = re.sub(r"`[^`]+`", "", stripped)
            cjk = sum(1 for ch in stripped if "\u4e00" <= ch <= "\u9fff")
            visible = sum(1 for ch in stripped if not ch.isspace())
            if visible >= 200 and cjk / max(visible, 1) < 0.15:
                report_non_cn = True
                await _emit(
                    task_id, "system",
                    {
                        "event": "report_lang_warning",
                        "reason": "report.md 不像中文 (CJK 占比 < 15%), skill 要求中文报告",
                        "cjk_chars": cjk,
                        "visible_chars": visible,
                        "report_path": report,
                    },
                    on_event,
                )
        except Exception:
            pass
    if report_non_cn:
        degraded = True

    # 契约缺漏率 > 30% 也算 degraded (case_c_contract 没触发自动续跑时, 紫标仍提醒)
    if contract_total > 0 and contract_missing_ratio > 0.3:
        degraded = True
        await _emit(
            task_id, "system",
            {
                "event": "contract_status",
                "skill": skill_name,
                "covered": contract_covered_n,
                "total": contract_total,
                "missing_ids": list(contract_missing_ids),
                "missing_ratio": round(contract_missing_ratio, 2),
            },
            on_event,
        )

    # Stop hook 被次数上限 bypass 过 → 任务实际没走完 hack 流水线, 紫标提醒用户
    # 文件由 pretool_guard.handle_stop 在阻断 >= _MAX_STOP_BLOCKS 时写入
    if (workdir / ".stop_hook_bypassed").exists():
        degraded = True
        try:
            bypass_msg = (workdir / ".stop_hook_bypassed").read_text(encoding="utf-8")[:200]
        except Exception:
            bypass_msg = ""
        await _emit(
            task_id, "system",
            {"event": "stop_hook_bypassed", "msg": bypass_msg},
            on_event,
        )

    # Layer 4: pretool_guard 检测到 sub-skill fuzz 浅 → 标 degraded + emit 提示
    # 文件由 pretool_guard._check_fuzz_depth 写入. soft block, 不影响任务完成状态.
    if (workdir / ".fuzz_shallow").exists():
        degraded = True
        try:
            fuzz_msg = (workdir / ".fuzz_shallow").read_text(encoding="utf-8")[:300]
        except Exception:
            fuzz_msg = ""
        await _emit(
            task_id, "system",
            {"event": "fuzz_shallow", "msg": fuzz_msg},
            on_event,
        )

    # 软警告 skill 缺失 (xss / open-redirect 等) → 标 degraded + emit 提示
    # 文件由 pretool_guard.handle_stop 在 HACK_SOFT_WARN 缺失时写入.
    # 这些 skill 不阻断退出 (避免拽走铁律 6), 但用户能看到模型漏了什么.
    if (workdir / ".skill_skipped").exists():
        degraded = True
        try:
            skipped = (workdir / ".skill_skipped").read_text(encoding="utf-8")[:200]
        except Exception:
            skipped = ""
        await _emit(
            task_id, "system",
            {"event": "skill_skipped_soft", "skipped": skipped,
             "msg": f"hack 模式可选 skill 未调起: {skipped} — 检查目标是否需要补测"},
            on_event,
        )

    # Layer 6 retrospective 浅 (调过但内容没自查关键词) → 紫标提醒
    # 文件由 pretool_guard._check_retrospective_depth soft 分支写入.
    # 不阻断 — 模型已经调了 retrospective 但内容质量不够.
    if (workdir / ".retrospective_shallow").exists():
        degraded = True
        try:
            retro_msg = (workdir / ".retrospective_shallow").read_text(encoding="utf-8")[:300]
        except Exception:
            retro_msg = ""
        await _emit(
            task_id, "system",
            {"event": "retrospective_shallow", "msg": retro_msg},
            on_event,
        )

    await store.update_task(
        task_id,
        status=new_status,
        finished_at=_t.time(),
        exit_code=rc,
        pending_question=needs_input_question,
        report_path=report,
        has_finding=has_finding,
        pid=None,
        mcp_calls=mcp_playwright_calls,
        py_web_calls=python_web_fallback_calls,
        degraded_to_python=1 if degraded else 0,
        nav_calls=nav_calls,
        unique_routes=len(unique_routes),
        interaction_calls=interaction_calls,
        network_req_calls=network_req_calls,
        contract_skill=skill_name or "",
        contract_total=contract_total,
        contract_covered=contract_covered_n,
        contract_missing_json=json.dumps(contract_missing_ids, ensure_ascii=False),
    )
    if degraded:
        await _emit(
            task_id, "system",
            {
                "event": "degraded",
                "reason": "no playwright MCP usage; python web fallback detected",
                "py_web_calls": python_web_fallback_calls,
                "mcp_calls": mcp_playwright_calls,
            },
            on_event,
        )
    if on_event:
        await on_event(task_id, "status", new_status)

    # 邮件通知: 仅在 done 且产出报告时触发
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
                task_id, "system", {"event": "mail_error", "error": repr(e)}
            )


def _kill_group(proc: asyncio.subprocess.Process) -> None:
    if proc.returncode is not None:
        return
    try:
        pgid = os.getpgid(proc.pid)
        try:
            os.killpg(pgid, signal.SIGCONT)  # unstop first so SIGTERM is delivered
        except (ProcessLookupError, PermissionError):
            pass
        os.killpg(pgid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        try:
            proc.terminate()
        except ProcessLookupError:
            pass


async def stop_task_proc(pid: int | None) -> None:
    if not pid:
        return
    try:
        pgid = os.getpgid(pid)
        try:
            os.killpg(pgid, signal.SIGCONT)  # unstop first so SIGTERM is delivered
        except (ProcessLookupError, PermissionError):
            pass
        os.killpg(pgid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        try:
            os.kill(pid, signal.SIGCONT)
        except (ProcessLookupError, PermissionError):
            pass
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass


async def soft_stop_task_proc(pid: int | None, *, timeout: float = 8.0) -> None:
    """优雅停 claude **本进程** (不动整个进程组), 给 followup/续跑用。

    - 只 SIGTERM claude pid 本身, 不发 killpg
    - claude 退出后, 它的 stdio 子进程 (playwright-mcp) 会因为 stdin EOF
      自然退出, 这是 stdio MCP 不可避免的; 但磁盘 .pw-profile 仍然保留,
      下一轮 claude 启动的新 playwright-mcp 会用同一 profile, 登录态延用
    - 比 stop_task_proc 多了 graceful 等待: 最多 timeout 秒, 超时再 SIGKILL
    """
    if not pid:
        return
    try:
        os.kill(pid, signal.SIGCONT)  # 防止之前被 SIGSTOP
    except (ProcessLookupError, PermissionError):
        pass
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        try:
            os.kill(pid, 0)
        except (ProcessLookupError, PermissionError):
            return  # 已退
        await asyncio.sleep(0.1)
    # 还活就强杀 (仍然只杀本进程, 不动进程组)
    try:
        os.kill(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
