"""Phase 执行审计: 从 .tool-events.jsonl 重建模型实际走过的 phase 轨迹,
检测"TodoWrite 标 completed 但同名 skill 从未被 Skill 工具调起"这类执行错觉。

设计动机 (顶级赏金视角):
- 现有 skill_contract 只校验 report.md 文本, 模型可以"口头声明执行"绕过
- TodoWrite 标 completed 不是执行证据 — Skill 工具调用记录才是
- ~/.claude/skills/hack/SKILL.md 已经写了"必须 Skill 工具调用"硬规则,
  这一层负责把规则落到执行验证, 让规则真正长牙齿

实现取舍:
- 不解析 transcript / 不依赖模型自报 — 只看 PostToolUse hook 落盘的事件
- 工具事件不可伪造, TodoWrite 也是事件, 两个事件流交叉验证
- 每个 hack 流水线必须的 skill 都列出, 没出现 = 跳过 (除非 N/A 写在报告里)
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

# 必经 skill 与 phase keywords 来自共享模块 hack_pipeline (单一真理来源)
from .hack_pipeline import (
    phase_for_todo as _phase_for_todo,
    required_skills_for_phase,
)

# hack 流水线 6 阶段 → 对应必须调用的 skill 列表 (DEPRECATED, 保留以防外部引用)
# 实际使用 hack_pipeline.required_skills_for_phase
_HACK_MANDATORY_SKILLS: dict[str, list[str]] = {
    "recon": ["recon"],
    "js": ["js-audit", "miniprogram-audit"],
    "auth": ["auth-bypass"],
    "sqli": ["sqli"],
    "biz": ["business-logic"],
    "validate": ["validate"],
    "report": ["report"],
}


@dataclass
class PhaseReport:
    """Phase 执行审计结果, runner 收尾根据 violations 决定是否续跑。"""

    skills_called: set[str] = field(default_factory=set)
    todo_completed_phases: set[int] = field(default_factory=set)
    violations: list[dict] = field(default_factory=list)
    # 若 hack 模式: 6 阶段中真正缺失了哪些 skill (用于 nudge 文本)
    missing_mandatory: list[str] = field(default_factory=list)

    def has_blocking_violation(self) -> bool:
        """决定是否触发自动续跑。任何 severity=high 即阻断收尾。"""
        return any(v.get("severity") == "high" for v in self.violations)


def _iter_events(events_path: Path):
    if not events_path.exists():
        return
    try:
        with events_path.open("r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except Exception:
                    continue
    except Exception:
        return


def audit(events_path: Path, skill_name: str | None) -> PhaseReport:
    """主入口: 扫 .tool-events.jsonl, 输出 phase 审计报告。

    skill_name 是首条 prompt 解析出的 slash command (如 'hack'). 只有 'hack'
    走完整 6 阶段强校验; 其他 skill 走轻量校验 (只检 TodoWrite 与 Skill 一致性)。
    """
    rep = PhaseReport()
    if not events_path.exists():
        return rep

    # 1) 收集所有 Skill 调用 + 最后一份 TodoWrite 状态
    last_todos: list[dict] | None = None
    for ev in _iter_events(events_path):
        tn = ev.get("tool_name")
        ti = ev.get("tool_input") or {}
        if tn == "Skill":
            sk = (ti.get("skill") or "").strip().lower()
            if sk:
                # 处理 'ms-office-suite:pdf' 这种带前缀的写法, 取冒号后的部分
                if ":" in sk:
                    sk = sk.split(":", 1)[1]
                rep.skills_called.add(sk)
        elif tn == "TodoWrite":
            todos = ti.get("todos")
            if isinstance(todos, list):
                last_todos = todos

    # 2) 把 TodoWrite 最终状态里的 completed phase 提取出来
    if last_todos:
        for td in last_todos:
            if not isinstance(td, dict):
                continue
            if td.get("status") != "completed":
                continue
            content = td.get("content") or td.get("activeForm") or ""
            ph = _phase_for_todo(content)
            if ph is not None:
                rep.todo_completed_phases.add(ph)

    # 3) 只有 hack 模式做 6 阶段强校验
    if (skill_name or "").lower() != "hack":
        # 轻量模式: 至少首条 slash command 对应的 skill 自己被调过
        if skill_name and skill_name.lower() not in rep.skills_called:
            rep.violations.append({
                "kind": "primary_skill_not_called",
                "severity": "high",
                "skill": skill_name,
                "msg": (
                    f"首条 prompt 走的是 /{skill_name}, 但 .tool-events.jsonl 里"
                    f"没有任何 Skill(skill='{skill_name}') 调用记录 — "
                    "极可能是从 system-reminder 内联重建后'假装'执行了。"
                ),
            })
        return rep

    # 4) hack 模式: 每个 completed phase 必须有对应 skill 调用证据
    # required_groups 是 list[list[str]], 每组都要至少有一个 skill 调过
    # 例: Phase 3 = [["sqli"], ["xss"]] → 必须 sqli 调过且 xss 调过
    # 例: Phase 1 = [["js-audit", "miniprogram-audit"]] → 二选一
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

    # 去重 missing_mandatory, 保持顺序
    seen: set[str] = set()
    dedup: list[str] = []
    for s in rep.missing_mandatory:
        if s not in seen:
            seen.add(s)
            dedup.append(s)
    rep.missing_mandatory = dedup

    return rep


def build_phase_skip_nudge(rep: PhaseReport, attempt: int, max_attempts: int) -> str:
    """命中 phase_skip 违规时, 给模型的纠偏 prompt。

    nudge 措辞要点:
    - 引用 hack/SKILL.md 的硬规则原话, 让模型清楚这不是新约束
    - 列出具体缺失的 skill 名 + Skill 工具调用样例
    - 强调"已知道 ≠ 已执行", 这是 hack/SKILL.md 写明的根本性执行错误
    """
    lines = [
        f"[Phase 执行审计不达标 / 第 {attempt}/{max_attempts} 次提醒]",
        "",
        "本轮 .tool-events.jsonl 重放显示: TodoWrite 把若干 phase 标了 completed, "
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
        lines.append(f"  □ Skill(skill=\"{s}\")  ← 加载 ~/.claude/skills/{s}/SKILL.md 并按其逐条执行")
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
