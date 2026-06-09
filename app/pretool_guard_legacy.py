"""PreToolUse / Stop hook 守卫: 当场阻断违规工具调用, 不再事后续跑。

设计动机 (顶级赏金 + 顶级开发视角):
- 旧方案 phase_audit + case_d 只在任务结束后判定, 模型可能花 1.5h 写出基于盲区的报告
- 新方案 PreToolUse hook 同步拦截: 模型连"跳过 Skill 写报告"这条路都走不通
- exit code 2 + stderr → claude 把 stderr 塞回模型上下文 → 工具被拒, 模型必须先补齐前置步骤

三层守卫:
- Layer 1 (TodoWrite 阻断): 把 phase 改 completed 时, 对应 skill 没出现过 → 拒
- Layer 2 (Stop 阻断): 任务即将结束前, hack 必经 skill 任一缺失 → 拒退出
- Layer 3 (shallow Skill 阻断): Skill 调完立刻又调下一个 Skill, 中间 < N 个真实工具动作 → 拒

调用约定:
- claude 通过 stdin 喂 hook payload (JSON)
- 当前 workdir 是任务工作目录, 可以读 .tool-events.jsonl 获取历史
- env var SECWEB_SKILL_NAME 由 runner.py 写 hook config 时注入, 标识首条 slash command
- exit 0 = 放行; exit 2 = 阻断 (stderr 是给模型看的纠偏说明)
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

_TOOL_EVENTS_FILE = ".tool-events.jsonl"

# Stop hook 阻断次数硬上限. 防止 stop_hook_active 协议字段被客户端漏处理时
# 模型 / dashboard 陷入死循环. 命中即放行 + 写 .stop_hook_bypassed 让 dashboard 紫标.
_MAX_STOP_BLOCKS = 3

# ───── Layer 4: Fuzz 深度家族多样性校验 ─────────────────────────────
# 核心思想: 死阈值 (≥20 payload) 不通用 (静态站浪费, API 站爆炸).
# 改为 "payload 家族多样性" — 把每类常见 payload 归到一个家族,
# 看模型是否真覆盖了多种攻击面, 而不是用 20 次相同探针刷量.
#
# 触发条件: 模型即将退出当前 sub-skill (调下一个 Skill 时), 才回扫上一个
# sub-skill 时段内的所有工具调用, 找 payload 字符串落到哪个家族.
#
# 设计: 每个 sub-skill 一个家族表 (signal → keyword 列表),
# 命中家族数 ≥ min_families 即通过. 阈值远小于家族总数 (留余地避免误伤).

_FUZZ_FAMILIES: dict[str, dict] = {
    "sqli": {
        "min_families": 5,  # 8 个家族里命中 ≥5 即通过
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
            "encoded_ip":       ["2130706433",  # 127.0.0.1 十进制
                                 "017700000001",  # 八进制
                                 "0x7f000001"],   # 十六进制
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
        # idor 不看 payload 家族, 看 ID 多样性 + 信号优先放行.
        # 阈值由 _idor_adaptive_threshold(workdir) 按交接表 ID 类端点数动态计算
        # (端点≤2 → 2, 端点 3-5 → 该数, 端点 ≥5 → 上限 5).
        # 已发现 IDOR 信号 (响应/日志含越权关键词) → 直接放行, 不再要求刷量.
        # min_unique_ids 字段保留作为 fallback (现在已不使用), 实际逻辑见 _check_fuzz_depth idor 分支.
        "min_unique_ids": 3,
        "id_pattern": r"[?&/]id=|/users?/|/orders?/|userId|orderId|fileId",
    },
}

# Layer 4 不做 hard block, 只做 soft block (写 .fuzz_shallow 标记 + degraded).
# 原因: payload 家族识别基于关键词匹配, 有误判风险, 不应当成阻断信号.
# 真正的阻断仍由 Layer 3 (shallow Skill) 担当, Layer 4 是质量提示.
_FUZZ_SHALLOW_FILE = ".fuzz_shallow"

# 必经 skill 与 phase keywords 来自共享模块, 避免三处定义漂移
# claude hook 把脚本作为独立文件跑 (不是 module import), 相对 import 会失败.
# 加 sys.path 让 hack_pipeline.py 能被同目录 import.
_THIS_DIR = Path(__file__).resolve().parent
if str(_THIS_DIR) not in sys.path:
    sys.path.insert(0, str(_THIS_DIR))
from hack_pipeline import (  # noqa: E402
    HACK_MUST_HAVE_EITHER,
    HACK_SOFT_WARN,
    hack_must_have_either,
    hack_soft_warn,
    phase_for_todo as _phase_for_todo,
    required_skills_for_phase,
    retro_keywords,
    shallow_ok_skills,
    fuzz_families_override,
)

# Layer 3: 一次 Skill 调用之后, 进入下一个 Skill 前必须出现的最小工具调用数
# 经验值: 真实跑一个 skill 至少有 8+ 次工具动作 (浏览器交互 / curl / fuzz)
# 低于此阈值视为 shallow Skill (只调用未执行)
_MIN_TOOLCALLS_BETWEEN_SKILLS = 8

# Layer 3 例外: 这些 skill 是辅助性的, 不强制深度
# validate 不在内: 它是 Phase 5 质量门 (7 问验证), 必须有真实测试动作.
# 之前误把 validate 列进豁免, 导致模型调 validate 后 0 次工具调用就退出 (实测 89e9eea).
# v1.1: 来源改为 skill_pipeline_loader, 用户可在 pipeline.json 的 shallow_ok_skills_extra 追加.
_SHALLOW_OK_SKILLS = shallow_ok_skills()


def _read_events(events_path: Path) -> list[dict]:
    if not events_path.exists():
        return []
    out: list[dict] = []
    try:
        with events_path.open("r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except Exception:
                    continue
    except Exception:
        pass
    return out


def _called_skills(events: list[dict]) -> set[str]:
    s: set[str] = set()
    for ev in events:
        if ev.get("tool_name") != "Skill":
            continue
        ti = ev.get("tool_input") or {}
        sk = (ti.get("skill") or "").strip().lower()
        if not sk:
            continue
        if ":" in sk:
            sk = sk.split(":", 1)[1]
        s.add(sk)
    return s


def _block(msg: str) -> None:
    """exit 2 + stderr → claude 把 stderr 塞回模型上下文。"""
    sys.stderr.write(msg)
    sys.exit(2)


def _allow() -> None:
    sys.exit(0)


def handle_pretool_todowrite(payload: dict, workdir: Path, skill_name: str) -> None:
    """Layer 1: TodoWrite 把 phase 改 completed 时, 对应 skill 必须出现过。"""
    if (skill_name or "").lower() != "hack":
        _allow()  # 非 hack 模式不强制 TodoWrite phase 校验
    todos = (payload.get("tool_input") or {}).get("todos")
    if not isinstance(todos, list):
        _allow()
    events = _read_events(workdir / _TOOL_EVENTS_FILE)
    called = _called_skills(events)
    missing: list[tuple[int, str]] = []  # (phase, missing_skill)
    for td in todos:
        if not isinstance(td, dict):
            continue
        if td.get("status") != "completed":
            continue
        ph = _phase_for_todo(td.get("content") or "")
        if ph is None:
            continue
        # required_groups: list of "任一即可"组. 每组都要至少有一个 skill 调过.
        # 例: Phase 3 = [["sqli"], ["xss"]] → 必须 sqli 调过且 xss 调过
        # 例: Phase 1 = [["js-audit", "miniprogram-audit"]] → 二选一
        required_groups = required_skills_for_phase(ph)
        if not required_groups:
            continue
        for group in required_groups:
            if not any(s in called for s in group):
                missing.append((ph, "/".join(group)))
    if not missing:
        _allow()
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
    _block("\n".join(lines))


def _idor_adaptive_threshold(workdir: Path) -> int:
    """根据交接表 endpoints.md 里 path:xxxId 类参数数量计算自适应阈值.

    规则:
      - 没找到交接表 → 默认 3 (兜底)
      - ≤2 个 ID 类端点 → 要求 2 个 ID (基线 + 越权)
      - 3-5 个 → 要求该数量
      - ≥5 个 → 上限 5 (避免大型 SaaS 强制刷 20+ 浪费时间)
    """
    # 找 *_endpoints.md 文件 (recon/js-audit 产出)
    candidates = list(workdir.glob("*endpoints*.md"))
    if not candidates:
        return 3
    id_count = 0
    seen_params: set[str] = set()
    for f in candidates:
        try:
            text = f.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        # 匹配 path:xxId / path:userId / path:fid 等
        # 也匹配 path:type,appId 这种逗号列表
        for m in re.finditer(r"path:([a-zA-Z_,]+)", text):
            for p in m.group(1).split(","):
                p = p.strip()
                if not p:
                    continue
                # 含 id (大小写不敏感) 视为 ID 类参数
                if re.search(r"[Ii]d", p) or p.lower() in ("fid", "uid", "pid"):
                    seen_params.add(p)
    id_count = len(seen_params)
    if id_count == 0:
        return 3  # 没识别到 ID 端点, 用默认值
    return max(2, min(5, id_count))


def _extract_payload_text(ev: dict) -> str:
    """从工具事件里抽出可能含 payload 的文本 (Bash command / browser_evaluate function /
    fetch URL 等). 不区分大小写, 不区分编码, 全部拼成一长串方便 substring match."""
    ti = ev.get("tool_input") or {}
    tn = ev.get("tool_name") or ""
    parts: list[str] = []
    # Bash: command 字段
    if tn == "Bash":
        parts.append(ti.get("command") or "")
    # browser_evaluate / browser_navigate: 各种 url/function 字段
    elif tn.startswith("mcp__playwright__"):
        for k in ("url", "function", "text", "value"):
            v = ti.get(k)
            if isinstance(v, str):
                parts.append(v)
    # 其他工具兜底: 把 tool_input 序列化为字符串扫
    else:
        try:
            parts.append(json.dumps(ti, ensure_ascii=False))
        except Exception:
            pass
    return "\n".join(parts)


def _check_fuzz_depth(events: list, last_skill_idx: int,
                      last_skill: str, workdir: Path) -> None:
    """Layer 4: 检测 sub-skill 时段的 payload 家族多样性.

    soft block: 只写 .fuzz_shallow 标记给 dashboard 标 degraded, 不阻断模型.
    原因: payload 关键词匹配有误判风险, 真正阻断由 Layer 3 担当.
    """
    # 优先用用户 pipeline.json 的 fuzz_families 配置, fallback 到内置默认
    # 用户可以在 ~/.claude/skills/<name>/pipeline.json 加新 skill 的家族表
    # (例如 graphql / nosql / xxe), 也可以覆盖现有的 sqli/xss/ssrf 阈值与 payload
    user_override = fuzz_families_override()
    cfg = user_override.get(last_skill) or _FUZZ_FAMILIES.get(last_skill)
    if not cfg:
        return  # 该 skill 没注册家族表, 不校验
    # 拼接 sub-skill 时段所有 payload 文本
    blob_parts: list[str] = []
    for ev in events[last_skill_idx + 1:]:
        blob_parts.append(_extract_payload_text(ev))
    blob = "\n".join(blob_parts)
    if not blob:
        return  # 没文本可扫, 静默放行 (Layer 3 已经查过工具调用数)

    # idor 走 ID 多样性 + 自适应阈值 + 信号优先放行
    if last_skill == "idor":
        ids_seen: set[str] = set()
        # 找 ?id=数字 / /users/数字 / /orders/数字 / "userId":数字 等
        for m in re.finditer(r"(?:[?&/](?:id|userId|orderId|fileId)=|/(?:users?|orders?|files?)/)(\w+)", blob):
            ids_seen.add(m.group(1))
        for m in re.finditer(r'"(?:id|userId|orderId|fileId)"\s*:\s*"?(\w+)', blob):
            ids_seen.add(m.group(1))
        # ─── 信号优先放行 ───
        # 已发现 IDOR 信号 (响应/日志含越权关键词) → 不再要求"试更多 ID"
        # 模型已经走铁律 6 横向扩展才是高 ROI, 强制刷 ID 反而拽走注意力
        idor_hit_keywords = (
            "vertical privilege", "horizontal privilege",
            "垂直越权", "水平越权",
            "IDOR confirmed", "IDOR 确认",
            "他人数据", "leaked another user",
            # 业务侧: 200 OK + 他人 ID 时, 报告里通常会有这些字样
            "未授权访问成功", "unauthorized access succeeded",
        )
        blob_kw = blob.lower()
        for kw in idor_hit_keywords:
            if kw.lower() in blob_kw:
                return  # 信号已成立, 静默放行
        # ─── 自适应阈值 ───
        # 扫交接表 endpoints.md 里 path:xxId / path:xxxId 类参数数量
        # 端点 ≤2 → 要求 2 个 ID (基线 + 越权), 端点 ≥5 → 要求 5 个 (上限)
        adaptive_min = _idor_adaptive_threshold(workdir)
        if len(ids_seen) >= adaptive_min:
            return
        msg = (f"idor fuzz 浅: 只测试了 {len(ids_seen)} 个不同 ID, "
               f"要求 ≥{adaptive_min} (按交接表 ID 类端点数自适应). "
               f"ID 见: {sorted(ids_seen)[:5]}")
        try:
            (workdir / _FUZZ_SHALLOW_FILE).write_text(msg, encoding="utf-8")
        except Exception:
            pass
        return

    # sqli/xss/ssrf/open-redirect 走 payload 家族多样性
    blob_lower = blob.lower()
    families = cfg["families"]
    hit_families: set[str] = set()
    for family_name, signals in families.items():
        for sig in signals:
            if sig.lower() in blob_lower:
                hit_families.add(family_name)
                break
    needed = cfg["min_families"]
    if len(hit_families) >= needed:
        return
    missing = sorted(set(families.keys()) - hit_families)
    msg = (f"{last_skill} fuzz 浅: 命中 {len(hit_families)}/{len(families)} "
           f"家族 (要求 ≥{needed}). 已命中: {sorted(hit_families)}. "
           f"缺失家族: {missing}")
    try:
        (workdir / _FUZZ_SHALLOW_FILE).write_text(msg, encoding="utf-8")
    except Exception:
        pass


def handle_pretool_skill(payload: dict, workdir: Path, skill_name: str) -> None:
    """Layer 3: 上一个 Skill 调用后必须有足够的真实工具动作, 否则视为 shallow Skill。

    例外方向修正 (实测 89e9eea 暴露的 bug):
    - 旧逻辑: next_skill 在 _SHALLOW_OK_SKILLS 中 → 直接放行 (错: 让 last_skill 浅尝逃生)
    - 新逻辑: 只看 last_skill 是不是辅助型, 不看 next_skill
      → business-logic 后调 validate 仍要查 business-logic 深度
      → validate 后调 report 才豁免 (因 validate 已被前移出辅助清单, 此处仅 retrospective/secknowledge/report)
    """
    ti = payload.get("tool_input") or {}
    next_skill = (ti.get("skill") or "").strip().lower()
    if ":" in next_skill:
        next_skill = next_skill.split(":", 1)[1]
    # 不再因 next_skill 是辅助型而豁免 — 这是 89e9eea 漏 business-logic 深度的根因
    events = _read_events(workdir / _TOOL_EVENTS_FILE)
    if not events:
        _allow()  # 第一次调 Skill, 没有前置历史
    # 找最近一次 Skill 调用 (排除 hack 这种调度器自身, hack 是入口不计)
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
            continue  # 调度器入口, 不算需要深度执行的 skill
        if sk in _SHALLOW_OK_SKILLS:
            continue
        last_skill_idx = i
        last_skill_name = sk
        break
    if last_skill_idx < 0:
        _allow()
    # 统计 last_skill_idx 之后的真实工具调用数 (Bash + browser_* + Read + Grep + Write 等)
    real_calls = 0
    skipped_tools = {"TodoWrite", "Skill"}  # 这两个是元工具, 不算执行深度
    for ev in events[last_skill_idx + 1:]:
        tn = ev.get("tool_name") or ""
        if tn in skipped_tools:
            continue
        real_calls += 1
    if real_calls >= _MIN_TOOLCALLS_BETWEEN_SKILLS:
        # Layer 3 通过, 进 Layer 4 fuzz 深度家族多样性检测
        _check_fuzz_depth(events, last_skill_idx, last_skill_name, workdir)
        _allow()
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
    _block("\n".join(lines))


def _path_in_blob(path: str, blob: str) -> bool:
    """检测交接表里的端点路径是否在事件流"真实 HTTP 请求"里出现过.

    早期 bug: 单纯 substring 匹配会让模型 echo/注释里提到路径也算"已测".
    例如模型 Bash 写 `echo "测试 /api/foo"` 但实际没 curl, substring
    匹配会误判为已测, 让漏洞溜走.

    现在: 路径必须在 HTTP 客户端调用上下文里出现 (curl/wget/fetch/axios/
    requests/urllib/browser_navigate 等同行内). 字符串提及不算.

    支持 {id} 等占位符: 把 /api/users/{id} 视作 /api/users/<任意>.
    """
    if not path or not blob:
        return False

    # 占位符 → 任意单词字符
    pattern = re.escape(path)
    pattern = re.sub(r'\\\{[a-zA-Z_][a-zA-Z_0-9]*\\\}', r'[\\w-]+', pattern)

    # path 起点判定: 路径必须前面是 hostname 边界 (host末尾字母 + /)、引号、空白、起始
    # 不能是另一个路径段的延续 (避免 /helper/v 误匹配 /helper)
    # 注意: hostname 后字母 \w + 路径 / 的情况是常态 (https://x.com/api/users)
    # 改用: 路径前不能是另一条 path 的延续 (即不能是单独 \w 紧跟 — \w 后必须是 / 或 .)
    # 简化: 路径开头 / 前面是 [非字母数字] 或字母数字+点(域名 TLD) 或字母数字+/
    # 更简洁: 路径**结尾**必须是 word boundary, 起点放宽
    path_re = pattern + r'(?![\w/])'

    # HTTP 客户端上下文: 找出"HTTP 请求 ... 路径"或"路径 ... HTTP 请求"的同段共现
    # 关键约束: 不能跨过 \necho / \nprint / \n# 这种非 HTTP 命令边界
    # — 防止"curl + 路径A 同 blob 后 echo 提到路径B 被误判已测"
    http_clients = (
        r'curl|wget|httpie?|fetch\(|axios\.[a-z]+|'
        r'requests\.[a-z]+|urllib\.request\.\w+|'
        r'browser_(?:navigate|evaluate|network_request)|'
        r'HttpClient|new Request'
    )
    # 不允许跨过的"非 HTTP 边界": 新行 + (echo|print|cat|grep|#|\necho)
    # 简化为: 同一逻辑行内 (\n 隔开 = 不同命令)
    # curl 命令多行 (反斜杠续行) 仍允许 — 因为 \\$ 是 shell 续行符
    http_context_res = [
        # HTTP 调用在前 → 路径在后, 同一行内 (含反斜杠续行)
        re.compile(
            r'(?:' + http_clients + r')'
            r'(?:[^\n]|\\\s*\n)*?'  # 同行 + 反斜杠续行
            + path_re,
            re.IGNORECASE,
        ),
        # 路径在前 → HTTP 调用在后 (URL='/foo'\ncurl ...): 允许跨 1 行
        re.compile(
            path_re + r'(?:[^\n]|\\\s*\n){0,200}?'
            r'\n?'
            r'(?:[^\n]){0,100}?'
            r'(?:' + http_clients + r')',
            re.IGNORECASE,
        ),
        # 形如 url = "/api/foo" / "endpoint": "/api/foo" 这种声明
        re.compile(
            r'(?:url|endpoint|api|path|target|URI|href|location)\s*[=:]\s*[\'"`]'
            + pattern + r'[\'"`]',
            re.IGNORECASE,
        ),
    ]

    try:
        for r in http_context_res:
            if r.search(blob):
                return True
    except re.error:
        pass

    # 全部 HTTP 上下文都没匹配 → 视作字符串提及, 不算已测
    return False


def _check_endpoint_coverage(workdir: Path) -> tuple[int, list[str], int]:
    """Layer 5: 扫 *_endpoints.md 找 [已测=✗] 的 P0 端点 + 双重证据校验.

    返回 (untested_count, sample_paths, adaptive_threshold).
    模型为通过 Layer 5 必须真去测每个端点
    (改 ✓ 同时该端点字符串要在 .tool-events.jsonl 出现过).

    设计动机:
    - hack/SKILL.md 铁律 3 (CRUD 全覆盖) + 铁律 6 (首漏扩展) 文档要求
      模型补完所有 [已测=✗], 但实测中模型可能跑完几个漏洞就退出, 留下
      大量未测端点. 单纯靠文档约束模型选择性执行, 必须用工具事件流交叉校验.
    - dashboard 4 层 hook 都没堵这个 — 只看工具调用次数 / payload 多样性.
    - Layer 5 直接读模型自己写的 [已测=✗] 标记, 用工具事件流交叉校验.

    防作弊 (双重证据):
    - 单证据: 交接表声明 [已测=✓] → 模型可改 ✗ 为 ✓ 不真测
    - 双证据: 交接表 [已测=✓] AND 该端点路径在 .tool-events.jsonl 真出现过

    自适应阈值: 按交接表 P0 行总数算, 小站不误伤, 大站不爆炸.
    """
    candidates = list(workdir.glob("*endpoints*.md"))
    if not candidates:
        return (0, [], 3)  # 没交接表, 兼容 EXEMPT-FULL / 纯静态站场景
    untested: list[str] = []
    total_marked_lines = 0

    # 检查 markdown 表格行的"已测"状态.
    # 模型生成的格式不稳定, 必须支持多种通用标记:
    # - [已测=✗] / [已测=X] / [已测=x] (带前缀, 中文)
    # - 裸 ✗ / ❌ / [ ] / [todo] (markdown 表格列里)
    # - tested=no / untested (英文 skill 套件)
    # 通过/N/A 类: ✓ / ✅ / [x] / [done] / N/A / 跳过
    UNTESTED_PATTERNS = (
        re.compile(r'\[已测=✗\]'),
        re.compile(r'\[已测=[Xx]\]'),
        re.compile(r'\[\s\]'),       # markdown TODO [ ]
        re.compile(r'\[todo\]', re.I),
        re.compile(r'\[untested\]', re.I),
        re.compile(r'tested\s*=\s*no', re.I),
    )
    PASSED_PATTERNS = (
        re.compile(r'\[已测=✓\]'),
        re.compile(r'\[已测=[Yy]\]'),
        re.compile(r'\[x\]'),         # markdown TODO [x]
        re.compile(r'\[done\]', re.I),
        re.compile(r'\[tested\]', re.I),
        re.compile(r'\[N/A.*?\]', re.I),
        re.compile(r'已测=N/A', re.I),
    )
    BARE_SYMBOL_RE = re.compile(r'^\s*([✗✓❌✅])\s*$')

    def _classify_line(line: str) -> str:
        """返回 'untested' / 'passed' / 'unknown'."""
        for p in PASSED_PATTERNS:
            if p.search(line):
                return 'passed'
        for p in UNTESTED_PATTERNS:
            if p.search(line):
                return 'untested'
        # 退化: 表格最后一个 cell 是裸 ✗/✓
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

    # 双重证据: 扫 .tool-events.jsonl 看哪些端点字符串其实出现过
    events = _read_events(workdir / _TOOL_EVENTS_FILE)
    blob_parts: list[str] = []
    for ev in events:
        ti = ev.get("tool_input") or {}
        for k in ("command", "function", "url", "body", "text"):
            v = ti.get(k)
            if isinstance(v, str):
                blob_parts.append(v)
    blob = "\n".join(blob_parts)
    truly_untested = [p for p in untested if not _path_in_blob(p, blob)]

    # 自适应阈值:
    # - 总行数 ≤4 (小站): 阈值 2 (允许漏 1 但不允许漏 2)
    # - 总行数 5-15 (中站): 阈值 3
    # - 总行数 ≥16 (大站): 阈值 max(3, 总数*15%) 防大站误伤
    if total_marked_lines <= 4:
        threshold = 2
    elif total_marked_lines <= 15:
        threshold = 3
    else:
        threshold = max(3, int(total_marked_lines * 0.15))

    return (len(truly_untested), truly_untested[:20], threshold)


def _check_retrospective_depth(workdir: Path,
                               events: list) -> tuple[str | None, str]:
    """Layer 6: 通用兜底 — retrospective 必须真跑 + 含自查证据.

    跟 Layer 5 不一样, 不依赖交接表格式 / URL 形态. 任何目标都适用.

    思路: hack/SKILL.md 铁律 6 / 铁律 3 / 报告前漏测自查 是文档约束,
    模型可能选择性执行. retrospective 是 hack 流水线的最后一步, 内容
    自带"漏测自查 / 横向扩展 / 失败模式"等深度反思. 强制 retrospective
    真跑 + 包含关键词, 等于让模型最后自我审查一遍.

    返回 (severity, msg). severity ∈ {None, "soft", "hard"}.
      None  → 通过
      soft  → 写 .skill_skipped 紫标 (不阻断)
      hard  → Stop hook decision=block (阻断退出)
    """
    # 找 retrospective 时段
    retro_start = -1
    for i, ev in enumerate(events):
        if ev.get("tool_name") != "Skill":
            continue
        sk = ((ev.get("tool_input") or {}).get("skill") or "").strip().lower()
        if ":" in sk:
            sk = sk.split(":", 1)[1]
        if sk == "retrospective":
            retro_start = i
            break

    if retro_start < 0:
        # 完全没调过 retrospective, soft warn (skill_skipped 已经会标)
        return (None, "")

    # retrospective 时段的真实工具调用数
    real_calls = 0
    for ev in events[retro_start + 1:]:
        tn = ev.get("tool_name") or ""
        if tn in ("TodoWrite", "Skill"):
            continue
        real_calls += 1

    # < 3 工具调用 = 几乎没干活, 敷衍
    if real_calls < 3:
        return ("hard", (
            f"retrospective 只产生了 {real_calls} 次工具调用 (要求 ≥3). "
            "retrospective 是 hack 流水线最后一步, 必须真跑包含: "
            "效率度量 / 失败模式记录 / 漏测自查 / 横向扩展确认. "
            "敷衍调用 = 浪费这一层质量门."
        ))

    # ≥3 调用但内容不含自查关键词 → soft warn (不阻断, 仅紫标)
    # 扫 workdir 自己的 .md (retrospective.md / report.md / memory 反馈记录)
    # 看有没有"横向扩展" / "未测端点" / "首漏扩展" / "铁律6" / "漏测" / "覆盖率" 等关键词.
    # 只扫 workdir, 不扫 ~/.claude memory — 历史 memory 含旧关键词会误判通过.
    # 关键词来自 hack_pipeline.retro_keywords() — 默认 + 用户 pipeline.json 追加 (取并集).
    self_audit_keywords = retro_keywords()
    text_blob = _check_retrospective_audit_text(workdir)

    has_self_audit = any(kw in text_blob for kw in self_audit_keywords)
    if not has_self_audit:
        return ("soft", (
            "retrospective 已跑 but 内容里没找到漏测自查关键词 "
            "(横向扩展 / 首漏扩展 / 漏测 / 覆盖率 / 铁律 6 等任一). "
            "建议在 retrospective 输出里明确回答: "
            "'本次找到的漏洞是否触发铁律 6 横向扩展? 哪些同模型端点未测?'"
        ))

    return (None, "")


def _check_retrospective_audit_text(workdir: Path) -> str:
    """收集本次任务的 retrospective 文本 — 只扫 workdir 自己的 md, 不扫 memory.
    历史 memory 含历史关键词会误判通过. 只信本次任务工作目录的产物.
    """
    text_blob = ""
    for f in workdir.glob("*.md"):
        try:
            text_blob += f.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            pass
    return text_blob



def handle_stop(payload: dict, workdir: Path, skill_name: str) -> None:
    """Layer 2: 任务即将结束前, hack 必经 skill 缺失 → 拒退出。"""
    if (skill_name or "").lower() != "hack":
        _allow()  # 非 hack 模式不强制
    # 防无限循环: claude hooks 协议会在第二次重入时设 stop_hook_active=True
    # 我们碰到这个标志直接放行, 让模型能正常退出 (信任协议层)
    if payload.get("stop_hook_active"):
        _allow()
    # 阻断次数硬上限: 即使 stop_hook_active 没设, workdir 计数 >= 3 也放行
    # 防御 claude 客户端版本未正确处理协议字段时的死循环
    count_file = workdir / ".stop_hook_count"
    try:
        prev = int(count_file.read_text().strip()) if count_file.exists() else 0
    except Exception:
        prev = 0
    if prev >= _MAX_STOP_BLOCKS:
        # 已经阻断过 N 次, 模型仍未补齐 — 放行但写一行警示进 events 让 dashboard 能感知
        try:
            (workdir / ".stop_hook_bypassed").write_text(
                f"Stop hook bypassed after {prev} blocks", encoding="utf-8"
            )
        except Exception:
            pass
        _allow()

    # ─── Hard check (Fix A): report skill 调过但 workdir 没真写出 report 文件 ───
    # 模型在 result.success 文本里声称"已写 report.md"是 system-reminder 自述, 不是工具调用记录.
    # 真实失败模式: 模型 result.success 文本声称"已写 report.md"但实际
    # 0 次 Write 工具调用 → _scan_report 找不到文件 → 续跑被 API 限流卡死.
    # 这条比 hack 必经清单更基础: report 调过 = 模型应该真写文件, 没写就拒退出.
    # EXEMPT 模式不触发 (report 没在 called 里, 不会进这里).
    events_for_report = _read_events(workdir / _TOOL_EVENTS_FILE)
    called_for_report = _called_skills(events_for_report)
    if "report" in called_for_report:
        # 接受 report.md / *_report.md / *report*.md 三种命名
        md_files = list(workdir.glob("*.md"))
        has_report_file = any(
            "report" in f.name.lower() and f.stat().st_size > 200
            for f in md_files
        )
        if not has_report_file:
            try:
                count_file.write_text(str(prev + 1), encoding="utf-8")
            except Exception:
                pass
            out = {
                "decision": "block",
                "reason": (
                    f"Stop 阻断 ({prev + 1}/{_MAX_STOP_BLOCKS}): 你调用了 Skill(report) 但当前 workdir "
                    "没有 report.md (或任何含 'report' 的 .md 文件 ≥200 字节).\n\n"
                    "在 result.success 文本里声称'已写 report.md'不算执行 — Skill 工具的内联自述 "
                    "≠ Write 工具的真实文件落盘. 必须真调用 Write 工具创建文件.\n\n"
                    "立即执行: Write(file_path='./report.md', content='<完整漏洞报告>') "
                    "把 report skill 产出的中文漏洞报告写入 workdir, 再退出."
                ),
            }
            sys.stdout.write(json.dumps(out, ensure_ascii=False))
            sys.exit(0)

    # ─── Layer 5 (Hard check): 端点覆盖率 ───
    # 实测漏 [已测=✗] 端点根因: 模型违反铁律 3/6 直接退出.
    # 扫交接表 *_endpoints.md, 任何标 [已测=✗] 且事件流里没真请求过的端点
    # → hard block, 列出来让模型回去补.
    # 自适应阈值: 小站 (≤4 行) 2, 中站 3, 大站 (≥16 行) 总数 15%.
    untested_count, untested_sample, untested_threshold = _check_endpoint_coverage(workdir)
    if untested_count >= untested_threshold:
        try:
            count_file.write_text(str(prev + 1), encoding="utf-8")
        except Exception:
            pass
        out = {
            "decision": "block",
            "reason": (
                f"Stop 阻断 ({prev + 1}/{_MAX_STOP_BLOCKS}): 端点交接表中有 "
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
            ),
        }
        sys.stdout.write(json.dumps(out, ensure_ascii=False))
        sys.exit(0)

    # ─── Layer 6 (通用兜底): retrospective 真跑 + 自查证据 ───
    # Layer 5 依赖 hack/SKILL.md 的交接表格式 (针对 RESTful + 标准输出).
    # Layer 6 不假设 URL 形态 / 标记格式, 只看 retrospective skill 的执行深度.
    # 任何目标 (GraphQL / 小程序 / 老 Spring) 都适用 — 真通用兜底.
    events_for_retro = _read_events(workdir / _TOOL_EVENTS_FILE)
    retro_severity, retro_msg = _check_retrospective_depth(workdir, events_for_retro)
    if retro_severity == "hard":
        try:
            count_file.write_text(str(prev + 1), encoding="utf-8")
        except Exception:
            pass
        out = {
            "decision": "block",
            "reason": (
                f"Stop 阻断 ({prev + 1}/{_MAX_STOP_BLOCKS}): {retro_msg}\n\n"
                "请重新调用 Skill(retrospective) 并真跑它的 9 个段落 — "
                "效率度量 / 技能命中率 / 误报率 / 失败模式 / 漏测自查 / 横向扩展确认. "
                "至少产生 ≥3 次 Read/Edit/Write 工具调用 (读 memory 历史 + 写本次反思)."
            ),
        }
        sys.stdout.write(json.dumps(out, ensure_ascii=False))
        sys.exit(0)
    elif retro_severity == "soft":
        # 不阻断, 写 .retrospective_shallow 让 runner emit 紫标
        try:
            (workdir / ".retrospective_shallow").write_text(
                retro_msg, encoding="utf-8"
            )
        except Exception:
            pass

    # 逃生口 (分级): hack/SKILL.md 的"唯一豁免"只豁免浏览器三件套, 不豁免 js-audit。
    # JS 里硬编码 AKSK / 内网地址 / SourceMap 是高价值攻击面, 即使纯静态站也要扫。
    #   EXEMPT-FULL: <理由>     → 真正全跳过 (DNS 失败 / 授权范围外 / 非 Web 协议且无 Web 入口)
    #                            连 recon 都跑不了的场景才用
    #   EXEMPT-DYNAMIC: <理由>  → 只跳过动态测试 (auth-bypass/sqli/business-logic 等),
    #                            仍强制 recon + js-audit + validate + report
    # 单纯写 EXEMPT: 视为 EXEMPT-DYNAMIC (向后兼容, 但仍要求 js-audit)
    report_path = workdir / "report.md"
    exempt_mode = ""  # "full" / "dynamic" / ""
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
        _allow()  # 全跳过, 信任模型已写明合法理由

    events = _read_events(workdir / _TOOL_EVENTS_FILE)
    called = _called_skills(events)
    # hack 必经 skill 列表来自共享模块 hack_pipeline (loader 转发).
    # 调函数而非用顶层常量, 避免 import 时缓存了过时配置.
    must_have_either: list[list[str]] = list(hack_must_have_either())
    # 注意 open-redirect 不在必经列表: 大多数 API 站没有 redirect 类参数, 强制跑会大量
    # EXEMPT 噪音. hack/SKILL.md Phase 3 已用"前提检查 → 跳过"模式声明它,
    # 模型在交接表有 redirect/callback/next 类参数时会自然调起.
    # EXEMPT-DYNAMIC 模式: 仍强制 recon + js-audit + validate + report
    # 原因: 纯静态站 JS 中可能硬编码凭证/内网地址, js-audit 不能跳过
    # xss 不进 DYNAMIC 必经: EXEMPT-DYNAMIC 多用于纯 API 站 / 无前端, xss 不适用
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
    # 软警告: HACK_SOFT_WARN 中缺失的 skill 写 .skill_skipped 文件给 runner emit 紫标
    # 不阻断退出, 不算入 _MAX_STOP_BLOCKS 计数. xss / open-redirect 在这里.
    soft_skipped: list[str] = []
    if exempt_mode != "full":  # EXEMPT-FULL 时所有 soft 检查也跳过
        for opts in hack_soft_warn():
            if not any(s in called for s in opts):
                soft_skipped.append(opts[0])
    if soft_skipped:
        try:
            (workdir / ".skill_skipped").write_text(
                ",".join(soft_skipped), encoding="utf-8"
            )
        except Exception:
            pass
    if not missing:
        _allow()
    # 阻断 → 累加计数
    try:
        count_file.write_text(str(prev + 1), encoding="utf-8")
    except Exception:
        pass
    # 用 Stop hook 的 JSON 输出格式: decision=block + reason
    out = {
        "decision": "block",
        "reason": (
            f"Stop 阻断 ({prev + 1}/{_MAX_STOP_BLOCKS}): hack 流水线必经 skill 仍有未调起的 — 不准结束。\n\n"
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
        ),
    }
    sys.stdout.write(json.dumps(out, ensure_ascii=False))
    sys.exit(0)


def main() -> None:
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except Exception:
        _allow()  # 解析失败, 不阻断主流程
    event = payload.get("hook_event_name") or ""
    tool_name = payload.get("tool_name") or ""
    skill_name = os.environ.get("SECWEB_SKILL_NAME", "").strip().lower()
    cwd = payload.get("cwd") or os.environ.get("SECWEB_WORKDIR") or os.getcwd()
    workdir = Path(cwd)

    if event == "PreToolUse":
        if tool_name == "TodoWrite":
            handle_pretool_todowrite(payload, workdir, skill_name)
        elif tool_name == "Skill":
            handle_pretool_skill(payload, workdir, skill_name)
        else:
            _allow()
    elif event == "Stop":
        handle_stop(payload, workdir, skill_name)
    else:
        _allow()


if __name__ == "__main__":
    main()
