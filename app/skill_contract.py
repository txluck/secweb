"""Skill 契约加载器: 从用户的 ~/.claude/skills/<name>/SKILL.md 提取关键铁律 / 强制门控,
拼成 dashboard 任务首条 prompt 末尾的"执行契约", 同时在收尾时校验 report.md 里
是否对每条契约给出了 [done] / [N/A: 原因] 之一的显式表态。

设计取舍 (顶级赏金视角):
- 不抽全部 75 项 checklist 喂给模型, 那会把注意力分散到打卡上, 反而降深挖能力
- 只抽**最高优先级的强制门控** + **铁律标号** + **报告前漏测自查中带硬动词的项**
- 模型可以合并表达 (e.g. [N/A x12: 静态站, 所有动态测试项不适用]) — 由 SYSTEM_APPEND
  侧的"目标自适应"段落允许
- 收尾扫 report.md 时, 缺失项作为下一轮 nudge 注入到同 session_id

为什么不让用户改 skill 文件: skill 升级 / 用户自己改 skill 都不影响 dashboard,
零侵入兼容性。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

# 兼容包导入与脚本独立调用两种场景 (与 pretool_guard.py / hack_pipeline.py 同)
_THIS_DIR = Path(__file__).resolve().parent
if str(_THIS_DIR) not in sys.path:
    sys.path.insert(0, str(_THIS_DIR))

from skill_pipeline_loader import command_to_skill_map  # noqa: E402

_SKILLS_ROOT = Path.home() / ".claude" / "skills"

# 命令到 skill 目录名的映射, 来自 skill_pipeline_loader (用户可在
# ~/.claude/skills/<name>/pipeline.json 的 command_to_skill 字段扩展).
# 没有用户配置时 fallback 到内置默认 (= v1.0 全集).
_COMMAND_TO_SKILL = command_to_skill_map()

# 高价值标记: 行里有这些词的 checklist 项, 才进强制契约
# 顶级猎人逻辑: 攻击面盲点 + 横向扩展 + 链式利用 是高赏金漏洞召回率的关键
_HARD_KEYWORDS = (
    "铁律", "强制", "必须", "禁止", "硬规则", "门控", "MUST", "REQUIRED",
    "首漏扩展", "横向扩展", "链式", "越权", "IDOR", "未授权",
    "SSO", "Token", "凭证", "鉴权", "认证", "权限",
    "Phase 0", "Phase 1", "Phase 2", "Phase 3", "Phase 4",
    # 各 skill 的高赏金核心动作
    "PoC", "复现", "盲注", "回显", "时间", "BENCHMARK",
    "竞态", "并发", "支付", "金额", "数量",
    "回调", "redirect", "open redirect", "OAuth",
    "原型污染", "DOM", "postMessage", "iframe", "CORS", "CSP",
    "SSRF", "metadata", "169.254", "内网",
    "SSTI", "{{", "模板",
    "JS", "chunk", "sourcemap", "bundle", "API",
    "endpoint", "端点", "接口",
    "枚举", "Fuzz", "fuzz", "变体", "矩阵", "WAF",
    "登录", "signup", "重置", "session", "JWT",
    "上传", "下载", "存储", "OSS", "S3",
    "暴露", "泄漏", "泄露", "敏感",
    "确认", "验证", "校验",
)

# 软排除: 这些主题已经在 dashboard 自己的 SYSTEM_APPEND 里强调过, 不再重复加进契约
_SOFT_EXCLUDE_KEYWORDS = (
    "中文报告", "report.md 必须", "playwright MCP", "browser_navigate",
    "browser_snapshot", "browser_network_requests",
)


def detect_slash_skill(prompt: str) -> str | None:
    """从用户 prompt 里识别第一个 slash command, 返回对应 skill 目录名。

    识别优先级 (v2.0 skill-agnostic):
    1. 命令在 pipeline.json 的 command_to_skill 映射表 → 直接返回映射值
    2. 命令不在映射表 → 检查 ~/.claude/skills/<cmd>/SKILL.md 是否存在, 存在就承认
       (这样用户装的任何 skill 无需改代码即可被识别)

    防御: prompt 里常见路径片段 (/Users/.../ 授权书.md) 会被简单的"第一个 /xxx"
    匹配错认成 /Users → users (SKILL.md 不存在, 会被兜底跳过). 用 finditer 扫所有
    候选, 首个命中即返回。
    """
    if not prompt:
        return None
    for m in re.finditer(r"/([a-z][a-z\-]*)\b", prompt):
        cmd = m.group(1).lower()
        # 1. 显式映射表
        if cmd in _COMMAND_TO_SKILL:
            return _COMMAND_TO_SKILL[cmd]
        # 2. 动态兜底: ~/.claude/skills/<cmd>/SKILL.md 存在 = 承认这是个合法 skill
        try:
            if (_SKILLS_ROOT / cmd / "SKILL.md").is_file():
                return cmd
        except OSError:
            continue
    return None


def _is_hard_item(line: str) -> bool:
    if any(k in line for k in _SOFT_EXCLUDE_KEYWORDS):
        return False
    return any(k in line for k in _HARD_KEYWORDS)


def extract_contract(skill_name: str, max_items: int = 18) -> list[str]:
    """从 skills/<name>/SKILL.md 抽出最高优先级的强制项, 返回简短清单。

    去重 + 截断 + 长度限制, 保证 prompt 不爆炸 (经验: 12-18 项最好,
    再多注意力分散, 再少漏盲区)。
    """
    f = _SKILLS_ROOT / skill_name / "SKILL.md"
    if not f.exists():
        return []
    try:
        text = f.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return []

    items: list[str] = []
    seen: set[str] = set()
    # 三种 checklist 格式:
    # - [ ] xxx  /  - [x] xxx  /  □ xxx  /  ### 铁律N: xxx  /  > ⚑ xxx
    patterns = [
        re.compile(r"^\s*[-*]\s*\[[\sxX]?\]\s+(.+?)$", re.M),
        re.compile(r"^\s*□\s+(.+?)$", re.M),
        re.compile(r"^\s*###\s+铁律\s*\d+\s*[:：]\s*(.+?)(?:\s*[\(（].*)?$", re.M),
        re.compile(r"^\s*>\s+⚑\s*\*\*?(.+?)\*?\*?[:：]\s*(.+?)$", re.M),
    ]
    for pat in patterns:
        for m in pat.finditer(text):
            line = m.group(1).strip()
            if pat.groups == 2:
                line = (m.group(1) + ": " + m.group(2)).strip()
            # 行长上限, 避免单行就把 prompt 撑大
            if len(line) > 120:
                line = line[:117] + "..."
            if not _is_hard_item(line):
                continue
            # 去重
            sig = re.sub(r"\s+", "", line.lower())[:60]
            if sig in seen:
                continue
            seen.add(sig)
            items.append(line)
            if len(items) >= max_items:
                break
        if len(items) >= max_items:
            break

    # 兜底: 没抽到 checklist 形式的项 → 用三级标题 (### N.M xxx) 作为攻击场景清单
    if not items:
        h_pat = re.compile(r"^\s*###\s+(\d+\.\d+\s+.+?)$", re.M)
        for m in h_pat.finditer(text):
            line = m.group(1).strip()
            if len(line) > 120:
                line = line[:117] + "..."
            sig = re.sub(r"\s+", "", line.lower())[:60]
            if sig in seen:
                continue
            seen.add(sig)
            items.append(line)
            if len(items) >= max_items:
                break

    return items


def build_skill_invocation_prefix(skill_name: str) -> str:
    """生成首条 user message 的前缀, 把 SKILL.md 全文 inline 注入 (CLI 等价模式).

    历史与根因 (按时间顺序):
      v1-v3: 提示 AI "请调 Skill 工具加载 SKILL.md" — 治标
      v4 极简: 修了 setting_sources 后认为足够 — 实测仍逊于 CLI
      v5 (本版, 2026-06-06): 直接 inline SKILL.md 全文, 复刻 CLI 客户端
        slash 自动展开机制.

    CLI vs Dashboard 真正差距 (实测 712d4892 session jsonl):
      CLI 客户端解析 /hack 后, 把 ~/.claude/skills/hack/SKILL.md 全文 (24KB)
      作为一条 user message 注入给模型 — SKILL.md 进入 **user 指令区**,
      模型按"用户给的指令"高优先级遵循. Dashboard 走 Skill 工具调用,
      SKILL.md 进入 **工具响应区**, 模型按"工具数据"中优先级处理 → 弱约束.

      实证: CLI 跑出 Open Redirect %23 / ID=866 状态变更 / 10 个 ERP,
      Dashboard 同 prompt 同 SKILL.md 同模型, 跑不出这些深层发现.
      差距来源不是 setting_sources, 是 SKILL.md 注入位置导致的 prompt 权重.

    本版做法:
      first_prompt 拼成 [SKILL.md 全文] + [USER PROMPT 标记] + 原 prompt,
      和 CLI 客户端 #2 消息字面等价. 模型起手就在 user 区看到完整流水线规则.

    通用性: 0 业务关键词, 仅按 skill_name 读对应文件, 任何 skill 任何目标通用.
    返回空串表示无 SKILL.md (skill_name 空 或文件不存在).
    """
    if not skill_name:
        return ""
    skill_md_path = Path.home() / ".claude" / "skills" / skill_name / "SKILL.md"
    if not skill_md_path.exists():
        # SKILL.md 不存在 — 退化到极简提醒, 不影响任务执行
        return (
            f"[Dashboard 模式] 你运行在 dashboard 后台模式, playwright MCP 已挂载.\n"
            f"用户用 slash command 触发了 /{skill_name} 流水线.\n"
            f"\n"
            f"[USER PROMPT]\n"
        )
    try:
        skill_content = skill_md_path.read_text(encoding="utf-8")
    except Exception:
        return ""
    # CLI 等价格式: "Base directory for this skill: <abs path>" + SKILL.md 全文
    # (与 CLI 客户端展开 slash 后注入给模型的 user message #2 字面等价)
    return (
        f"Base directory for this skill: {skill_md_path.parent}\n\n"
        f"{skill_content}\n\n"
        f"---\n"
        f"[Dashboard 模式说明] 你运行在 dashboard 后台 (SDK + playwright MCP).\n"
        f"上方是 /{skill_name} 完整 SKILL.md, 已直接进入你的上下文 — 严格按它执行.\n"
        f"SKILL.md 里要求调 Skill(skill=\"<sub>\") 时, 真的用 Skill 工具调,\n"
        f"加载子 skill 后同样严格按其内容执行.\n"
        f"\n"
        f"[USER PROMPT]\n"
    )


def build_contract_prompt(skill_name: str) -> str:
    """生成用于追加到首条 user message 末尾的"执行契约"段落。

    返回空串表示不应当追加 (skill 不存在 / 没抽到强制项)。
    """
    items = extract_contract(skill_name)
    if not items:
        return ""
    numbered = "\n".join(f"  C{i+1}. {it}" for i, it in enumerate(items))
    return (
        "\n\n[EXECUTION CONTRACT — derived from "
        f"~/.claude/skills/{skill_name}/SKILL.md]\n"
        "下列是从你将要调用的 skill 里抽出的**强制门控 / 铁律 / 高价值动作**。"
        "在 report.md 末尾加一节 \"## 契约执行清单\", 对每条 C-id 给出以下三种之一:\n"
        "  - `Cx: [done]` + 一句话证据指针 (例: '见报告漏洞 #2 PoC' / '见 coverage.md')\n"
        "  - `Cx: [N/A: <理由>]` (例: '未登录态站, 无横向越权场景')\n"
        "  - `Cx: [skip: <理由>]` (例: 'WAF 强阻断, 已切到下一目标')\n"
        "**允许合并表达** (例: 'C3-C8: [N/A x6: 静态站, 所有动态测试项不适用]'),\n"
        "但每条都必须有显式回应, 不能静默漏。dashboard 会校验。\n"
        f"{numbered}"
    )


def parse_report_contract_status(report_text: str, skill_name: str) -> dict:
    """扫 report.md 找契约执行清单, 返回 {covered_ids: set, missing_ids: set}。

    覆盖判定: 行里出现 'C<n>:' 或 'C<n>-C<m>:' 形式 (合并表达).
    """
    items = extract_contract(skill_name)
    if not items:
        return {"covered_ids": set(), "missing_ids": set(), "total": 0}
    total = len(items)
    covered: set[int] = set()
    # 单点: C5: [done]
    for m in re.finditer(r"C(\d+)\s*[:：]", report_text):
        try:
            i = int(m.group(1))
            if 1 <= i <= total:
                covered.add(i)
        except Exception:
            pass
    # 范围: C3-C8: ...
    for m in re.finditer(r"C(\d+)\s*-\s*C?(\d+)\s*[:：]", report_text):
        try:
            a = int(m.group(1)); b = int(m.group(2))
            if a > b:
                a, b = b, a
            for i in range(a, b + 1):
                if 1 <= i <= total:
                    covered.add(i)
        except Exception:
            pass
    all_ids = set(range(1, total + 1))
    return {
        "covered_ids": covered,
        "missing_ids": all_ids - covered,
        "total": total,
    }
