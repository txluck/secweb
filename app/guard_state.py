"""单任务的全部守卫状态 + 6 层守卫的纯 Python 实现.

历史问题:
- pretool_guard.py 是个独立 CLI, 由 claude CLI 的 hook 子进程协议 (`sh -c command`)
  拉起, 通过 stdin (JSON) + exit code (0=放行 / 2=阻断) + stderr (纠偏文本) + stdout
  (Stop hook 的 {decision,reason} JSON) 与父进程通信.
- 跨进程同步靠 5 个磁盘 marker 文件 (.stop_hook_count / .stop_hook_bypassed /
  .fuzz_shallow / .skill_skipped / .retrospective_shallow), runner.py 收尾时回扫
  这些文件决定 degraded 紫标.
- 任意 marker 漏写就让对应紫标失效, 任意路径解析差异就让规则失效.

收敛:
- 现在 SDK 模式 hooks 是 in-process Python 函数, 直接调本模块的 check_* 返回 Decision
- 状态全部在内存 GuardState 对象里, 不再写 marker 文件
- Layer 1-6 保持原语义, 行为零回归
- pretool_guard.py 的 CLI 入口在 commit3c 中作为 _legacy 保留 (兼容性), 但 SDK 路径
  不再用它

Decision 类型:
- Allow: 放行
- Deny(msg): PreToolUse 拒绝工具调用, msg 进模型上下文
- Block(reason): Stop hook 拒退出, reason 进模型上下文
- SoftWarn(tag, msg): 不阻断, 写到 GuardState 对应字段, runner 收尾 emit 紫标
"""
from __future__ import annotations

import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Union

# 共享配置层 (来自用户 ~/.claude/skills/<name>/pipeline.json + 内置默认)
# hack_pipeline 是薄外观, skill_pipeline_loader 是 cache.
_THIS_DIR = Path(__file__).resolve().parent
if str(_THIS_DIR) not in sys.path:
    sys.path.insert(0, str(_THIS_DIR))
from hack_pipeline import (  # noqa: E402
    hack_must_have_either,
    hack_soft_warn,
    phase_for_todo,
    required_skills_for_phase,
    retro_keywords,
    shallow_ok_skills,
    fuzz_families_override,
)

from .metrics import TaskMetrics


# ───── Decision 类型 ─────────────────────────────────────────────

@dataclass
class Allow:
    pass


@dataclass
class Deny:
    """PreToolUse: 拒绝该次工具调用 (模型看到 msg, 必须改路径)."""
    msg: str


@dataclass
class Block:
    """Stop hook: 拒退出 (模型看到 reason, 必须继续干活)."""
    reason: str


@dataclass
class SoftWarn:
    """不阻断, 但记录到 state.<tag>, 收尾时 emit 紫标."""
    tag: str           # 'fuzz_shallow' / 'retro_shallow' / 'skill_skipped'
    msg: str


Decision = Union[Allow, Deny, Block, SoftWarn]


# ───── GuardState ────────────────────────────────────────────────

@dataclass
class GuardState:
    """单任务的全部守卫态. SDK hook 闭包持有此对象.

    替代旧版 5 个 marker 文件:
    - stop_block_count   ↔ .stop_hook_count
    - stop_bypassed      ↔ .stop_hook_bypassed
    - fuzz_shallow       ↔ .fuzz_shallow
    - soft_skipped       ↔ .skill_skipped
    - retro_shallow      ↔ .retrospective_shallow
    """

    task_id: str
    workdir: Path
    skill_name: str | None = None  # 首条 prompt 的 slash command (小写)

    # 工具事件流 (in-memory, PostToolUse hook 累加)
    # 每条 = {"tool_name", "tool_input", "tool_response", "ts"}
    tool_events: list[dict] = field(default_factory=list)
    # 被调过的 skill 集合 (从 tool_events 里 Skill 工具调用累计)
    skills_called: set[str] = field(default_factory=set)
    # TodoWrite 最后一次状态里 completed 的 phase 集合
    todo_completed_phases: set[int] = field(default_factory=set)

    metrics: TaskMetrics = field(default_factory=TaskMetrics)

    # Stop hook 阻断计数 (= 旧 .stop_hook_count). 上限 _MAX_STOP_BLOCKS.
    stop_block_count: int = 0
    stop_bypassed: str | None = None      # 上限后写入, 紫标用

    # Layer 4 soft warn
    fuzz_shallow: str | None = None
    # Layer 6 soft warn
    retro_shallow: str | None = None
    # 横向扩展提醒已发过 (一次性提醒, 不死循环)
    lateral_reminder_sent: bool = False
    # Stop hook soft warn (xss / open-redirect 缺失)
    soft_skipped: list[str] = field(default_factory=list)

    # pause/unpause 控制 (SDK 化后底层简化, 不再 SIGSTOP)
    paused: bool = False


# Stop hook 阻断次数硬上限. 防止 stop_hook_active 协议字段被漏处理时死循环.
_MAX_STOP_BLOCKS = 3

# Layer 3: 一次 Skill 调用之后, 进入下一个 Skill 前必须出现的最小工具调用数
_MIN_TOOLCALLS_BETWEEN_SKILLS = 8

# Layer 3 例外: 这些 skill 是辅助性的, 不强制深度 (来自 skill_pipeline_loader)
_SHALLOW_OK_SKILLS = shallow_ok_skills()


# ───── Layer 4 fuzz 家族表 (内置默认, 用户可在 pipeline.json 覆盖) ──

_FUZZ_FAMILIES: dict[str, dict] = {
    "sqli": {
        "min_families": 5,
        "families": {
            "quote_injection":  ["'", '"', "%27", "%22", "&#39;", "&apos;"],
            "boolean_logic":    ["OR 1=1", "AND 1=2", " or 1", " and 1",
                                 "OR%201=1", "OR%20%271%27=%271"],
            "time_blind":       ["SLEEP(", "BENCHMARK", "WAITFOR DELAY",
                                 "pg_sleep", "DBMS_LOCK"],
            "error_based":      ["extractvalue(", "updatexml(", "convert(",
                                 "exp(~", "floor(rand", "geometrycollection("],
            "comment_truncate": ["/*", "--", "#", "%23", "/**/"],
            "encoded":          ["%2527", "%2520", "&#x27;", "0x27"],
            "case_mixed":       ["SeLeCt", "sLeEp", "UnIoN", "AnD", "Or"],
            "stacked":          ["; SELECT", "; DROP", "; INSERT", "; UPDATE"],
        },
    },
    "xss": {
        "min_families": 3,
        "families": {
            "tag_injection":    ["<script", "<svg", "<img", "<iframe", "<body"],
            "event_handler":    ["onerror=", "onload=", "onclick=",
                                 "onmouseover=", "onfocus=", "onblur="],
            "protocol":         ["javascript:", "data:text/html", "vbscript:"],
            "encoded":          ["&#x", "%3Cscript", "&#60;", "\\u003c"],
            "template":         ["{{", "${", "<%=", "{%"],
            "dom_source":       ["location.hash", "document.referrer",
                                 "window.name", "postMessage"],
        },
    },
    "ssrf": {
        "min_families": 3,
        "families": {
            "metadata":         ["169.254.169.254", "metadata.google",
                                 "instance/metadata", "100.100.100.200",
                                 "metadata.tencent"],
            "localhost":        ["127.0.0.1", "localhost", "0.0.0.0",
                                 "[::1]", "127.1"],
            "encoded_ip":       ["2130706433", "017700000001", "0x7f000001"],
            "protocol_switch":  ["file://", "gopher://", "dict://",
                                 "ldap://", "ftp://"],
            "dns_rebind":       [".xip.io", ".nip.io", ".sslip.io",
                                 ".oastify.com", ".burpcollab"],
            "userinfo_bypass":  ["@127.0.0.1", "@localhost", "@169.254"],
        },
    },
    "open-redirect": {
        "min_families": 3,
        "families": {
            "userinfo":         ["@attacker", "@evil", "@example.com"],
            "protocol":         ["//attacker", "\\\\attacker", "\\/\\/"],
            "encoded":          ["%2F%2F", "%5C%5C", "%2f%2eattacker"],
            "case_mixed":       ["HTTP://", "Http://", "JaVaScRiPt:"],
            "unicode_idn":      ["xn--", "\\u002f", "\\u005c"],
        },
    },
    "idor": {
        "min_unique_ids": 3,
        "id_pattern": r"[?&/]id=|/users?/|/orders?/|userId|orderId|fileId",
    },
}


# ───── 内部工具函数 ─────────────────────────────────────────────

def _skill_name_in_events(state: GuardState) -> set[str]:
    """从 state.tool_events 重算被调 skill 集合 (同时刷新 state.skills_called).

    幂等: hook 已经在 observe_tool 里 append, 这里只是用于复盘.
    """
    s: set[str] = set()
    for ev in state.tool_events:
        if ev.get("tool_name") != "Skill":
            continue
        ti = ev.get("tool_input") or {}
        sk = (ti.get("skill") or "").strip().lower()
        if not sk:
            continue
        if ":" in sk:
            sk = sk.split(":", 1)[1]
        s.add(sk)
    state.skills_called = s
    return s


def _idor_adaptive_threshold(workdir: Path) -> int:
    """根据 *_endpoints.md 里 ID 类参数数量算自适应阈值. 2-5."""
    candidates = list(workdir.glob("*endpoints*.md"))
    if not candidates:
        return 3
    seen: set[str] = set()
    for f in candidates:
        try:
            text = f.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        for m in re.finditer(r"path:([a-zA-Z_,]+)", text):
            for p in m.group(1).split(","):
                p = p.strip()
                if not p:
                    continue
                if re.search(r"[Ii]d", p) or p.lower() in ("fid", "uid", "pid"):
                    seen.add(p)
    n = len(seen)
    if n == 0:
        return 3
    return max(2, min(5, n))


def _extract_payload_text(ev: dict) -> str:
    """从工具事件抽出可能含 payload 的字符串拼接."""
    ti = ev.get("tool_input") or {}
    tn = ev.get("tool_name") or ""
    parts: list[str] = []
    if tn == "Bash":
        parts.append(ti.get("command") or "")
    elif tn.startswith("mcp__playwright__"):
        for k in ("url", "function", "text", "value"):
            v = ti.get(k)
            if isinstance(v, str):
                parts.append(v)
    else:
        try:
            import json as _json
            parts.append(_json.dumps(ti, ensure_ascii=False))
        except Exception:
            pass
    return "\n".join(parts)


def _path_in_blob(path: str, blob: str) -> bool:
    """路径必须在 HTTP 客户端调用上下文里出现才算"已测".

    防御: echo/注释里的路径不算 (实测发现的漏洞).
    """
    if not path or not blob:
        return False
    pattern = re.escape(path)
    pattern = re.sub(r'\\\{[a-zA-Z_][a-zA-Z_0-9]*\\\}', r'[\\w-]+', pattern)
    path_re = pattern + r'(?![\w/])'
    http_clients = (
        r'curl|wget|httpie?|fetch\(|axios\.[a-z]+|'
        r'requests\.[a-z]+|urllib\.request\.\w+|'
        r'browser_(?:navigate|evaluate|network_request)|'
        r'HttpClient|new Request'
    )
    res = [
        re.compile(
            r'(?:' + http_clients + r')'
            r'(?:[^\n]|\\\s*\n)*?'
            + path_re,
            re.IGNORECASE,
        ),
        re.compile(
            path_re + r'(?:[^\n]|\\\s*\n){0,200}?'
            r'\n?'
            r'(?:[^\n]){0,100}?'
            r'(?:' + http_clients + r')',
            re.IGNORECASE,
        ),
        re.compile(
            r'(?:url|endpoint|api|path|target|URI|href|location)\s*[=:]\s*[\'"`]'
            + pattern + r'[\'"`]',
            re.IGNORECASE,
        ),
    ]
    try:
        for r in res:
            if r.search(blob):
                return True
    except re.error:
        pass
    return False


def _check_endpoint_coverage(state: GuardState) -> tuple[int, list[str], int]:
    """Layer 5: 扫 *_endpoints.md 找 [已测=✗] 的 P0 端点 + 双重证据.

    返回 (untested_count, sample_paths, adaptive_threshold).
    """
    candidates = list(state.workdir.glob("*endpoints*.md"))
    if not candidates:
        return (0, [], 3)
    untested: list[str] = []
    total_marked_lines = 0

    UNTESTED_PATTERNS = (
        re.compile(r'\[已测=✗\]'),
        re.compile(r'\[已测=[Xx]\]'),
        re.compile(r'\[\s\]'),
        re.compile(r'\[todo\]', re.I),
        re.compile(r'\[untested\]', re.I),
        re.compile(r'tested\s*=\s*no', re.I),
    )
    PASSED_PATTERNS = (
        re.compile(r'\[已测=✓\]'),
        re.compile(r'\[已测=[Yy]\]'),
        re.compile(r'\[x\]'),
        re.compile(r'\[done\]', re.I),
        re.compile(r'\[tested\]', re.I),
        re.compile(r'\[N/A.*?\]', re.I),
        re.compile(r'已测=N/A', re.I),
    )
    BARE_SYMBOL_RE = re.compile(r'^\s*([✗✓❌✅])\s*$')

    def _classify_line(line: str) -> str:
        for p in PASSED_PATTERNS:
            if p.search(line):
                return 'passed'
        for p in UNTESTED_PATTERNS:
            if p.search(line):
                return 'untested'
        cells = [c.strip() for c in line.split("|") if c.strip()]
        if cells:
            last = cells[-1]
            m = BARE_SYMBOL_RE.match(last)
            if m:
                if m.group(1) in ('✗', '❌'):
                    return 'untested'
                if m.group(1) in ('✓', '✅'):
                    return 'passed'
            if last.startswith(('✓', '✅', '[x]', '[X]')):
                return 'passed'
            if last.startswith(('✗', '❌', '[ ]')):
                return 'untested'
        return 'unknown'

    for f in candidates:
        try:
            text = f.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        for line in text.splitlines():
            if not line.lstrip().startswith("|"):
                continue
            klass = _classify_line(line)
            if klass == 'unknown':
                continue
            total_marked_lines += 1
            if klass != 'untested':
                continue
            cells = [c.strip() for c in line.split("|") if c.strip()]
            if not cells:
                continue
            path_cell = cells[0]
            for m in re.finditer(r"(/[a-zA-Z][a-zA-Z0-9/_.-]*)", path_cell):
                untested.append(m.group(1))

    if not untested:
        return (0, [], 3)

    # 双重证据: 扫 tool_events 里 HTTP 上下文中真出现的路径
    blob_parts: list[str] = []
    for ev in state.tool_events:
        ti = ev.get("tool_input") or {}
        for k in ("command", "function", "url", "body", "text"):
            v = ti.get(k)
            if isinstance(v, str):
                blob_parts.append(v)
    blob = "\n".join(blob_parts)
    truly_untested = [p for p in untested if not _path_in_blob(p, blob)]

    if total_marked_lines <= 4:
        threshold = 2
    elif total_marked_lines <= 15:
        threshold = 3
    else:
        threshold = max(3, int(total_marked_lines * 0.15))

    return (len(truly_untested), truly_untested[:20], threshold)


def _check_retrospective_depth(state: GuardState) -> tuple[str | None, str]:
    """Layer 6: retrospective 真跑 + 自查关键词.

    返回 (severity, msg). severity ∈ {None, 'soft', 'hard'}.
    """
    retro_start = -1
    for i, ev in enumerate(state.tool_events):
        if ev.get("tool_name") != "Skill":
            continue
        sk = ((ev.get("tool_input") or {}).get("skill") or "").strip().lower()
        if ":" in sk:
            sk = sk.split(":", 1)[1]
        if sk == "retrospective":
            retro_start = i
            break
    if retro_start < 0:
        return (None, "")

    real_calls = 0
    for ev in state.tool_events[retro_start + 1:]:
        tn = ev.get("tool_name") or ""
        if tn in ("TodoWrite", "Skill"):
            continue
        real_calls += 1

    if real_calls < 3:
        return ("hard", (
            f"retrospective 只产生了 {real_calls} 次工具调用 (要求 ≥3). "
            "retrospective 是 hack 流水线最后一步, 必须真跑包含: "
            "效率度量 / 失败模式记录 / 漏测自查 / 横向扩展确认. "
            "敷衍调用 = 浪费这一层质量门."
        ))

    # ≥3 调用但内容不含自查关键词 → soft warn (只扫 workdir 自己的 md)
    text_blob = ""
    for f in state.workdir.glob("*.md"):
        try:
            text_blob += f.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            pass
    keywords = retro_keywords()
    if not any(kw in text_blob for kw in keywords):
        return ("soft", (
            "retrospective 已跑 but 内容里没找到漏测自查关键词 "
            "(横向扩展 / 首漏扩展 / 漏测 / 覆盖率 / 铁律 6 等任一). "
            "建议在 retrospective 输出里明确回答: "
            "'本次找到的漏洞是否触发铁律 6 横向扩展? 哪些同模型端点未测?'"
        ))

    return (None, "")


# ───── PostToolUse: 累计 state ──────────────────────────────────

def observe_tool(state: GuardState, tool_name: str, tool_input: Any, tool_response: Any = None) -> None:
    """PostToolUse hook 调一次. 累计 state.tool_events / metrics / skills_called / todos."""
    if not tool_name:
        return
    ev = {
        "tool_name": tool_name,
        "tool_input": tool_input or {},
        "tool_response": tool_response,
    }
    state.tool_events.append(ev)
    state.metrics.observe(tool_name, tool_input)
    # 同步刷新 Skill 调用集合
    if tool_name == "Skill":
        ti = tool_input or {}
        sk = (ti.get("skill") if isinstance(ti, dict) else "")
        sk = (sk or "").strip().lower()
        if ":" in sk:
            sk = sk.split(":", 1)[1]
        if sk:
            state.skills_called.add(sk)
    elif tool_name == "TodoWrite":
        ti = tool_input or {}
        todos = ti.get("todos") if isinstance(ti, dict) else None
        if isinstance(todos, list):
            completed = set()
            for td in todos:
                if not isinstance(td, dict):
                    continue
                if td.get("status") != "completed":
                    continue
                ph = phase_for_todo(td.get("content") or td.get("activeForm") or "")
                if ph is not None:
                    completed.add(ph)
            state.todo_completed_phases = completed


# ───── PreToolUse(TodoWrite): Layer 1 ───────────────────────────

def check_pretool_todowrite(payload: dict, state: GuardState) -> Decision:
    """Layer 1: TodoWrite 把 phase 改 completed 时, 对应 skill 必须出现过.

    只对 hack skill 启用 (其他 skill 不强制 phase 校验).
    """
    if (state.skill_name or "").lower() != "hack":
        return Allow()
    todos = (payload.get("tool_input") or {}).get("todos")
    if not isinstance(todos, list):
        return Allow()

    called = _skill_name_in_events(state)
    missing: list[tuple[int, str]] = []
    for td in todos:
        if not isinstance(td, dict):
            continue
        if td.get("status") != "completed":
            continue
        ph = phase_for_todo(td.get("content") or "")
        if ph is None:
            continue
        required_groups = required_skills_for_phase(ph)
        if not required_groups:
            continue
        for group in required_groups:
            if not any(s in called for s in group):
                missing.append((ph, "/".join(group)))

    if not missing:
        return Allow()

    lines = [
        "TodoWrite 阻断: 你试图把以下 phase 标 completed, 但对应 skill 从未通过 Skill 工具调起。",
        "这违反 ~/.claude/skills/hack/SKILL.md 第一条硬规则:",
        "",
        "  > 流水线中所有 /skill-name 必须通过 Skill 工具显式调用执行",
        "  > 判断标准: 没有出现 Skill 工具调用记录 = 没有执行该技能, 没有例外",
        "",
        "缺失项 (按 phase):",
    ]
    for ph, need in missing:
        lines.append(f"  - Phase {ph}: 必须先 Skill(skill=\"{need.split('/')[0]}\")")
    lines += [
        "",
        "请先调起这些 Skill (每一个都加载对应的 ~/.claude/skills/<name>/SKILL.md 并按其执行),",
        "完成实际测试后再回来更新 TodoWrite。不允许把 TodoWrite 当作执行声明。",
    ]
    return Deny("\n".join(lines))


# ───── PreToolUse(Skill): Layer 3 + Layer 4 ─────────────────────

def check_pretool_skill(payload: dict, state: GuardState) -> Decision:
    """Layer 3: 上一个 Skill 后必须有 ≥8 个真实工具调用, 否则 shallow Skill 拒绝.

    Layer 4 fuzz 家族多样性在 Layer 3 通过后顺路检查 (soft block, 写 state.fuzz_shallow).
    """
    ti = payload.get("tool_input") or {}
    next_skill = (ti.get("skill") or "").strip().lower()
    if ":" in next_skill:
        next_skill = next_skill.split(":", 1)[1]

    events = state.tool_events
    if not events:
        return Allow()  # 第一次调 Skill, 没有前置

    # 找最近一次 Skill 调用 (排除 hack / bug-bounty 入口, shallow_ok 也跳过)
    last_skill_idx = -1
    last_skill_name = ""
    for i in range(len(events) - 1, -1, -1):
        ev = events[i]
        if ev.get("tool_name") != "Skill":
            continue
        sk = ((ev.get("tool_input") or {}).get("skill") or "").strip().lower()
        if ":" in sk:
            sk = sk.split(":", 1)[1]
        if sk in ("hack", "bug-bounty"):
            continue
        if sk in _SHALLOW_OK_SKILLS:
            continue
        last_skill_idx = i
        last_skill_name = sk
        break

    if last_skill_idx < 0:
        return Allow()

    # 统计 last_skill_idx 之后的真实工具调用 (排除 TodoWrite / Skill)
    real_calls = 0
    for ev in events[last_skill_idx + 1:]:
        tn = ev.get("tool_name") or ""
        if tn in ("TodoWrite", "Skill"):
            continue
        real_calls += 1

    if real_calls >= _MIN_TOOLCALLS_BETWEEN_SKILLS:
        # Layer 3 通过, 进 Layer 4 fuzz 深度
        _check_fuzz_depth_inplace(events, last_skill_idx, last_skill_name, state)
        return Allow()

    lines = [
        f"Skill 阻断: 上一次调用了 Skill(skill=\"{last_skill_name}\"), "
        f"但之后只产生了 {real_calls} 次真实工具调用 "
        f"(要求 ≥ {_MIN_TOOLCALLS_BETWEEN_SKILLS}), 视为 shallow Skill。",
        "",
        f"shallow Skill 是高频失败模式: 调起 {last_skill_name} 后没有真正执行其内部 checklist,",
        "立刻又调下一个 Skill, 等于把 skill 当作打卡而非执行。",
        "",
        f"请回到 Skill(skill=\"{last_skill_name}\") 的实际工作:",
        "  - 按 ~/.claude/skills/" + last_skill_name + "/SKILL.md 列的步骤逐条执行",
        "  - 用浏览器 / Bash / curl 真正发请求 + 收响应",
        "  - 把发现写进 report.md 对应章节",
        "",
        f"完成上一个 skill 后再调 Skill(skill=\"{next_skill}\")。",
    ]
    return Deny("\n".join(lines))



def _check_lateral_expansion(state: GuardState) -> tuple[bool, list[str]]:
    """检测铁律 6 横向扩展是否真的执行了.

    设计:
      场景 1 - report.md 没漏洞 → 跳过 (无可扩展对象)
      场景 2 - report.md 有漏洞 + 至少出现过 success:true →
        看后续是否对 endpoints.md 中**同前缀其他写操作端点**也用相同条件重测.
      "同前缀" = path 前 3 段相同 (如 /<service>/<role>/<resource>/* 全部一组).
      "重测过" = .tool-events 里 path 出现 ≥1 次.

    通用性: 仅按 path 动词 + path 前缀分组, 0 业务关键词.

    返回 (need_remind, missed_paths). need_remind=True 时才需要 block 提醒.
    """
    workdir = state.workdir
    report_path = workdir / "report.md"

    # 场景 1: 报告不存在 / 报告无漏洞证据 → 跳过
    if not report_path.exists():
        return (False, [])
    try:
        report_text = report_path.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return (False, [])

    has_findings = bool(
        re.search(r'success["\']?\s*:\s*["\']?true', report_text, re.I)
        or re.search(r'^##.*漏洞\s*[#一二三四五六七八九十\d]', report_text, re.M)
        or re.search(r'CVSS\s*[:：]?\s*\d', report_text)
    )
    if not has_findings:
        return (False, [])

    # 找 endpoints.md
    candidates = list(workdir.glob("*endpoints*.md"))
    if not candidates:
        return (False, [])

    # 抽 endpoints.md 里所有端点 + ✓ 标注 + 是否写操作
    # 通用写操作动词
    WRITE_VERBS = (
        "write", "update", "delete", "create", "add", "remove",
        "dispose", "verify", "reply", "clear", "modify", "edit",
        "save", "insert", "upload", "cancel", "approve", "reject",
    )
    PASSED_RE = re.compile(r'(\[已测=✓\]|✓|✅|\[done\]|\[tested\])')

    # all_endpoints: [(path, is_write, is_passed)]
    all_endpoints: list[tuple[str, bool, bool]] = []
    for f in candidates:
        try:
            text = f.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        for line in text.splitlines():
            if not line.lstrip().startswith("|"):
                continue
            cells = [c.strip() for c in line.split("|") if c.strip()]
            if not cells:
                continue
            m = re.search(r"(/[a-zA-Z][a-zA-Z0-9/_.{}-]*)", cells[0])
            if not m:
                continue
            path = m.group(1)
            is_write = any(v in path.lower() for v in WRITE_VERBS)
            is_passed = bool(PASSED_RE.search(line))
            all_endpoints.append((path, is_write, is_passed))

    if not all_endpoints:
        return (False, [])

    # 按 path 前缀 (前 3 段) 分组. 例: /<service>/<role>/<resource>/* 同组.
    def _prefix_of(p: str) -> str:
        parts = [s for s in p.split("/") if s]
        return "/".join(parts[:3]) if len(parts) >= 3 else "/".join(parts)

    blob = "\n".join(
        str(e.get("tool_input", {}))
        + str(e.get("tool_response", ""))[:2000]
        for e in state.tool_events
        if e.get("tool_name") in (
            "Bash",
            "mcp__playwright__browser_navigate",
            "mcp__playwright__browser_evaluate",
            "mcp__playwright__browser_network_request",
            "mcp__playwright__browser_network_requests",
            "mcp__playwright__browser_run_code_unsafe",
            "WebFetch",
        )
    )

    groups: dict[str, list[tuple[str, bool, bool]]] = {}
    for path, is_write, is_passed in all_endpoints:
        groups.setdefault(_prefix_of(path), []).append(
            (path, is_write, is_passed)
        )

    # 触发条件: 组内有任意端点 (含读) 已测 ✓ → 该组的写端点必须全部被测过.
    # "测过" = 在 .tool-events 出现 ≥1 次 (不要求 success:true, 至少打了请求).
    missed: list[str] = []
    for prefix, items in groups.items():
        # 组内有任意 ✓ → 启用横向扩展检查
        if not any(passed for _, _, passed in items):
            continue
        # 检查所有写端点是否在事件流出现过
        for path, is_write, is_passed in items:
            if not is_write:
                continue
            if is_passed:
                continue  # 已测的不要求扩展 (已经覆盖)
            needle = re.sub(r"\{[a-zA-Z_]+\}", "", path)
            if len(needle) < 4:
                continue
            if needle not in blob:
                missed.append(path)

    return (len(missed) > 0, missed[:10])


def _check_fuzz_depth_inplace(
    events: list, last_skill_idx: int, last_skill: str, state: GuardState
) -> None:
    """Layer 4 (soft): 检测 sub-skill 时段的 payload 家族多样性.

    命中 → 写 state.fuzz_shallow (供 runner 收尾 emit 紫标).
    """
    user_override = fuzz_families_override()
    cfg = user_override.get(last_skill) or _FUZZ_FAMILIES.get(last_skill)
    if not cfg:
        return

    blob_parts: list[str] = []
    for ev in events[last_skill_idx + 1:]:
        blob_parts.append(_extract_payload_text(ev))
    blob = "\n".join(blob_parts)
    if not blob:
        return

    # idor: ID 多样性 + 自适应阈值 + 信号优先放行
    if last_skill == "idor":
        ids_seen: set[str] = set()
        for m in re.finditer(
            r"(?:[?&/](?:id|userId|orderId|fileId)=|/(?:users?|orders?|files?)/)(\w+)",
            blob,
        ):
            ids_seen.add(m.group(1))
        for m in re.finditer(r'"(?:id|userId|orderId|fileId)"\s*:\s*"?(\w+)', blob):
            ids_seen.add(m.group(1))
        idor_hit_keywords = (
            "vertical privilege", "horizontal privilege",
            "垂直越权", "水平越权",
            "IDOR confirmed", "IDOR 确认",
            "他人数据", "leaked another user",
            "未授权访问成功", "unauthorized access succeeded",
        )
        blob_kw = blob.lower()
        for kw in idor_hit_keywords:
            if kw.lower() in blob_kw:
                return  # 信号成立, 放行
        adaptive_min = _idor_adaptive_threshold(state.workdir)
        if len(ids_seen) >= adaptive_min:
            return
        msg = (
            f"idor fuzz 浅: 只测试了 {len(ids_seen)} 个不同 ID, "
            f"要求 ≥{adaptive_min} (按交接表 ID 类端点数自适应). "
            f"ID 见: {sorted(ids_seen)[:5]}"
        )
        state.fuzz_shallow = msg
        return

    # sqli/xss/ssrf/open-redirect: payload 家族多样性
    blob_lower = blob.lower()
    families = cfg["families"]
    hit: set[str] = set()
    for fname, signals in families.items():
        for sig in signals:
            if sig.lower() in blob_lower:
                hit.add(fname)
                break
    needed = cfg["min_families"]
    if len(hit) >= needed:
        return
    missing = sorted(set(families.keys()) - hit)
    msg = (
        f"{last_skill} fuzz 浅: 命中 {len(hit)}/{len(families)} "
        f"家族 (要求 ≥{needed}). 已命中: {sorted(hit)}. "
        f"缺失家族: {missing}"
    )
    state.fuzz_shallow = msg


# ───── Stop hook: Layer 2 / 5 / 6 ───────────────────────────────

def check_stop(payload: dict, state: GuardState) -> Decision:
    """Stop hook 三层: report-file 检查 + Layer 5 端点覆盖 + Layer 6 retrospective + Layer 2 必经 skill.

    - stop_hook_active=True: 协议层防循环, 放行
    - stop_block_count >= _MAX_STOP_BLOCKS: 上限放行, 写 state.stop_bypassed
    - 其他情况按层级 hard block (Stop hook 的 {decision:'block'} 协议)
    """
    if (state.skill_name or "").lower() != "hack":
        return Allow()

    if payload.get("stop_hook_active"):
        return Allow()

    if state.stop_block_count >= _MAX_STOP_BLOCKS:
        state.stop_bypassed = f"Stop hook bypassed after {state.stop_block_count} blocks"
        return Allow()

    workdir = state.workdir
    called = _skill_name_in_events(state)

    # Hard check: report skill 调过但 workdir 没真写出 report 文件
    if "report" in called:
        md_files = list(workdir.glob("*.md"))
        has_report = any(
            "report" in f.name.lower() and f.stat().st_size > 200
            for f in md_files
        )
        if not has_report:
            state.stop_block_count += 1
            return Block(
                f"Stop 阻断 ({state.stop_block_count}/{_MAX_STOP_BLOCKS}): "
                "你调用了 Skill(report) 但当前 workdir 没有 report.md "
                "(或任何含 'report' 的 .md 文件 ≥200 字节).\n\n"
                "在 result.success 文本里声称'已写 report.md'不算执行 — Skill 工具的内联自述 "
                "≠ Write 工具的真实文件落盘. 必须真调用 Write 工具创建文件.\n\n"
                "立即执行: Write(file_path='./report.md', content='<完整漏洞报告>') "
                "把 report skill 产出的中文漏洞报告写入 workdir, 再退出."
            )

    # Layer 5: 端点覆盖率
    untested_count, untested_sample, untested_threshold = _check_endpoint_coverage(state)
    if untested_count >= untested_threshold:
        state.stop_block_count += 1
        return Block(
            f"Stop 阻断 ({state.stop_block_count}/{_MAX_STOP_BLOCKS}): 端点交接表中有 "
            f"{untested_count} 个 [已测=✗] 端点未在事件流出现过 "
            f"(阈值 ≥{untested_threshold}, 按交接表大小自适应) — 违反 "
            f"hack/SKILL.md 铁律 3 (CRUD 全覆盖) + 铁律 6 (首漏扩展).\n\n"
            "未测端点 (前 20):\n"
            + "\n".join(f"  - {p}" for p in untested_sample)
            + "\n\n"
            "策略 (二选一):\n"
            "1. 真去测: 用相同凭证/绕过技巧扫这些端点, 在 .tool-events.jsonl "
            "留下请求记录 (curl/Bash/browser_evaluate 任一即可),"
            " 同时把交接表对应行 [已测=✗] 改成 [已测=✓] + 一句话结果.\n"
            "2. 写明跳过原因: 在交接表对应行加 [已测=N/A: <理由>] "
            "(如 '需 admin 账号, 当前低权限不可达' / '环境阻断 503'),"
            " 不能保留 [已测=✗] 同时直接退出.\n\n"
            "原则: 一个漏洞 = 系统性缺陷信号. 找到一个写操作越权后, "
            "用同凭证测同模型 (同服务/同前缀) 的所有 CRUD 方法 "
            "(create/update/delete/list/export 等) 是高 ROI 必做."
        )

    # Layer 6: retrospective 真跑 + 自查
    retro_severity, retro_msg = _check_retrospective_depth(state)
    if retro_severity == "hard":
        state.stop_block_count += 1
        return Block(
            f"Stop 阻断 ({state.stop_block_count}/{_MAX_STOP_BLOCKS}): {retro_msg}\n\n"
            "请重新调用 Skill(retrospective) 并真跑它的 9 个段落 — "
            "效率度量 / 技能命中率 / 误报率 / 失败模式 / 漏测自查 / 横向扩展确认. "
            "至少产生 ≥3 次 Read/Edit/Write 工具调用 (读 memory 历史 + 写本次反思)."
        )
    elif retro_severity == "soft":
        state.retro_shallow = retro_msg

    # EXEMPT 模式 (报告第一行声明全跳过 / 仅跳动态)
    report_path = workdir / "report.md"
    exempt_mode = ""
    if report_path.exists():
        try:
            head = report_path.read_text(encoding="utf-8", errors="ignore")[:2000]
            if re.search(r"^EXEMPT-FULL\s*[:：]", head, re.M):
                exempt_mode = "full"
            elif re.search(r"^EXEMPT(-DYNAMIC)?\s*[:：]", head, re.M):
                exempt_mode = "dynamic"
        except Exception:
            pass
    if exempt_mode == "full":
        return Allow()

    # 横向扩展提醒 (一次性, 仅在已找到漏洞但未对同前缀其他写端点重测时触发)
    # 解决 SKILL.md 铁律 6 文字劝告 AI 选择性执行的问题. 通用 — 仅按 path
    # 动词 + 前缀分组, 0 业务关键词.
    if not state.lateral_reminder_sent:
        need_remind, missed = _check_lateral_expansion(state)
        if need_remind:
            state.lateral_reminder_sent = True
            state.stop_block_count += 1
            sample = "\n".join(f"  - {p}" for p in missed[:8])
            return Block(
                f"Stop 阻断 ({state.stop_block_count}/{_MAX_STOP_BLOCKS}): "
                "已检测到漏洞 (success:true / CVSS), 但 hack/SKILL.md 铁律 6 "
                "(首漏扩展) 未真做.\n\n"
                f"未覆盖的同前缀写端点 (前 8 个):\n{sample}\n\n"
                "━━━ 关键: 横向扩展 ≠ 重新无认证测试 ━━━\n"
                "你已确认的漏洞产出了什么 (token/cookie/凭证/绕过技巧)?\n"
                "→ 用**这些产出的攻击条件**去测上面的端点, 不是用'无认证'重测.\n"
                "  例: 漏洞 #1 泄露了 X 系统的 token Y → 用 Y 调上面所有端点,\n"
                "      不是再次试无 token 然后写'需要 session, 无法越权'.\n"
                "  例: 漏洞 #1 是 SSO callback 绕过 → 用绕过链调上面所有端点.\n"
                "  例: 漏洞 #1 是任意上传 → 用上传后产出的文件 ID 调上面端点.\n"
                "\n"
                "如果你拿当前漏洞链产出的凭证测了, 这些端点仍然返回 11500000/"
                "401 等认证错误 (即跨服务认证模型独立, 凭证不共用), 才是真正的"
                "'无法越权'. 不要拿无认证响应当作'已测过'.\n\n"
                "二选一 (尊重你的判断):\n"
                "  方案 A — 真做横向扩展: 提取已确认漏洞链中的凭证/绕过条件,\n"
                "    用 curl/Bash/browser_evaluate 重发上述端点, 拿到响应后\n"
                "    更新 endpoints.md + 报告新发现.\n"
                "  方案 B — 写明跳过原因: 在 endpoints.md 对应行加\n"
                "    [已测=N/A: <理由>] (如 '需 admin 账号' / '环境阻断' /\n"
                "    '已用泄露 token X 测过, 服务端独立校验 session').\n\n"
                "原则: 同一团队同一鉴权缺陷在系统内重复出现. 第一个漏洞产出的\n"
                "凭证/绕过链路, 才是横向扩展的弹药."
            )

    must_have_either: list[list[str]] = list(hack_must_have_either())
    if exempt_mode == "dynamic":
        must_have_either = [
            ["recon"],
            ["js-audit", "miniprogram-audit"],
            ["validate"],
            ["report"],
        ]

    missing: list[str] = []
    for opts in must_have_either:
        if not any(s in called for s in opts):
            missing.append(opts[0])

    # 软警告 (xss / open-redirect)
    soft_skipped: list[str] = []
    if exempt_mode != "full":
        for opts in hack_soft_warn():
            if not any(s in called for s in opts):
                soft_skipped.append(opts[0])
    if soft_skipped:
        state.soft_skipped = soft_skipped

    if not missing:
        return Allow()

    state.stop_block_count += 1
    return Block(
        f"Stop 阻断 ({state.stop_block_count}/{_MAX_STOP_BLOCKS}): "
        "hack 流水线必经 skill 仍有未调起的 — 不准结束。\n\n"
        f"当前模式: {'EXEMPT-DYNAMIC (仍强制 recon+js-audit+validate+report)' if exempt_mode == 'dynamic' else '完整流水线'}\n\n"
        "缺失 (优先按此顺序补齐):\n"
        + "\n".join(f"  □ Skill(skill=\"{s}\")" for s in missing)
        + "\n\n"
        "每一个都必须真的调起 (加载对应 SKILL.md 并按其执行), 不许在文本里复述。\n\n"
        "如果当前目标真的不适用整条流水线, 在 report.md 第一段加一行 (二选一):\n"
        "  EXEMPT-FULL: <理由>     ← 完全跳过 (DNS 失败 / 授权范围外 / 非 Web 协议且无 Web 入口)\n"
        "  EXEMPT-DYNAMIC: <理由>  ← 只跳过动态测试, 仍跑 recon+js-audit+validate+report\n"
        "                            (用于纯静态站 — JS 里仍可能硬编码凭证/内网地址)\n"
        "Stop hook 检到对应 EXEMPT 行后会按级别放行/收紧。"
    )


# ───── PhaseReport (替代 phase_audit.audit) ───────────────────

@dataclass
class PhaseReport:
    """Phase 执行审计结果, runner 收尾根据 violations 决定是否续跑."""
    skills_called: set[str] = field(default_factory=set)
    todo_completed_phases: set[int] = field(default_factory=set)
    violations: list[dict] = field(default_factory=list)
    missing_mandatory: list[str] = field(default_factory=list)

    def has_blocking_violation(self) -> bool:
        return any(v.get("severity") == "high" for v in self.violations)


def audit_phases(state: GuardState) -> PhaseReport:
    """从 state.tool_events 重建 phase 轨迹 (替代旧 phase_audit.audit).

    检测: TodoWrite 标 completed 但对应 skill 从未通过 Skill 工具调起.
    """
    rep = PhaseReport()
    _skill_name_in_events(state)  # 刷新 skills_called
    rep.skills_called = set(state.skills_called)
    rep.todo_completed_phases = set(state.todo_completed_phases)

    skill_name = state.skill_name

    # 非 hack: 只验首条 prompt 的 slash command 自己被调过
    if (skill_name or "").lower() != "hack":
        if skill_name and skill_name.lower() not in rep.skills_called:
            rep.violations.append({
                "kind": "primary_skill_not_called",
                "severity": "high",
                "skill": skill_name,
                "msg": (
                    f"首条 prompt 走的是 /{skill_name}, 但事件流里"
                    f"没有任何 Skill(skill='{skill_name}') 调用记录 — "
                    "极可能是从 system-reminder 内联重建后'假装'执行了。"
                ),
            })
        return rep

    # hack: 每个 completed phase 必须有对应 skill 调用证据
    for phase in sorted(rep.todo_completed_phases):
        required_groups = required_skills_for_phase(phase)
        if not required_groups:
            continue
        for group in required_groups:
            if not any(s in rep.skills_called for s in group):
                missing_label = "/".join(group)
                rep.violations.append({
                    "kind": "phase_completed_without_skill",
                    "severity": "high",
                    "phase": phase,
                    "missing_skill": missing_label,
                    "msg": (
                        f"Phase {phase} 标 completed, 但 "
                        f"Skill(skill in {group}) 整个会话从未被调起 — "
                        "这违反 hack/SKILL.md 的'必须 Skill 工具显式调用'硬规则。"
                    ),
                })
                rep.missing_mandatory.append(group[0])

    # 去重保序
    seen: set[str] = set()
    dedup: list[str] = []
    for s in rep.missing_mandatory:
        if s not in seen:
            seen.add(s)
            dedup.append(s)
    rep.missing_mandatory = dedup
    return rep


def build_phase_skip_nudge(rep: PhaseReport, attempt: int, max_attempts: int) -> str:
    """phase_skip 违规时的纠偏 prompt (替代 phase_audit.build_phase_skip_nudge)."""
    lines = [
        f"[Phase 执行审计不达标 / 第 {attempt}/{max_attempts} 次提醒]",
        "",
        "本轮事件流重放显示: TodoWrite 把若干 phase 标了 completed, "
        "但对应的 skill 从未通过 Skill 工具实际调用 — 这违反 ~/.claude/skills/hack/"
        "SKILL.md 第一条硬规则: ",
        "",
        "  > 流水线中所有 /skill-name 必须通过 Skill 工具显式调用执行",
        "  > 判断标准: 没有出现 Skill 工具调用记录 = 没有执行该技能, 没有例外",
        "  > AI 可能从 system-reminder 或上下文中看到技能内容, 并误认为'已知道 = 已执行'",
        "",
        "审计违规清单:",
    ]
    for v in rep.violations:
        if v.get("kind") == "phase_completed_without_skill":
            lines.append(
                f"  - Phase {v['phase']} 缺 Skill(skill='{v['missing_skill']}') 调用记录"
            )
        elif v.get("kind") == "primary_skill_not_called":
            lines.append(
                f"  - 首条 prompt 用了 /{v['skill']} 但 Skill 工具未调起"
            )
    lines += [
        "",
        "现在请按以下顺序补齐 (每一个都必须真的调 Skill 工具, 不要在文本里复述):",
    ]
    for s in rep.missing_mandatory:
        lines.append(
            f"  □ Skill(skill=\"{s}\")  ← 加载 ~/.claude/skills/{s}/SKILL.md 并按其逐条执行"
        )
    lines += [
        "",
        "执行完每个 skill 后, 把该 skill 真正产出的发现/证据/无发现理由补进 report.md "
        "对应章节, 不允许只写一句'已执行'。",
        "",
        "如果某个 skill 在当前目标上确实不适用 (例如纯静态站对 sqli 不适用), 仍然必须先 "
        "Skill(skill=\"<name>\") 调起一次让它走自己的'前提检查 → 止损'路径, 再在 report.md 里"
        "写明 N/A 理由, 不能跳过 Skill 调用本身。",
    ]
    return "\n".join(lines)


# ───── SDK Hook payload 适配器 ─────────────────────────────────

def decision_to_sdk_pretool(decision: Decision) -> dict:
    """Decision → SDK PreToolUse hook 返回值."""
    if isinstance(decision, Allow):
        return {}
    if isinstance(decision, Deny):
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": decision.msg or "guard 阻断",
            },
            "systemMessage": (decision.msg or "guard 阻断")[:500],
        }
    # Block / SoftWarn 不应在 PreToolUse 出现 (Stop hook 才有 block 语义)
    return {}


def decision_to_sdk_stop(decision: Decision) -> dict:
    """Decision → SDK Stop hook 返回值."""
    if isinstance(decision, Allow):
        return {}
    if isinstance(decision, Block):
        return {"decision": "block", "reason": decision.reason}
    return {}
