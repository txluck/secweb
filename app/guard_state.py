"""单任务的全部守卫状态 + skill-agnostic 守卫的纯 Python 实现.

v2.0 skill-agnostic 重构:
- 移除所有 `!= "hack"` 硬短路 — 守卫是否生效完全由 skill 的 pipeline.json 决定
- 删除 hack 专属的 Layer 3 (shallow) / Layer 5 (端点覆盖) / Layer 6 (retrospective) —
  这些是 hack pipeline 的具体规则, 应写在 hack/SKILL.md 或 hack/hooks/ 里, 不该硬编码在 secweb
- 保留通用守卫: Layer 1 (TodoWrite phase 校验) / Layer 2 (Stop 必经 skill) / report.md 落盘检查

启用条件:
- Layer 1: `pipeline.json.phase_required` 非空 → 校验; 空 → 放行
- Layer 2: `pipeline.json.stop_hook_required` 非空 → 校验; 空 → 放行
- report.md 落盘: 只要 skill(report) 被调过就检查, 通用

历史 (v1.x): 4 层 gate 全部 hardcode hack, 所有非 hack skill 直接跳过. 那份逻辑现在
移到 hack/pipeline.json + hack/SKILL.md (通过 skill_invocation_prefix 内联进 context) 里.

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

# 共享配置层 (来自用户 ~/.claude/skills/<name>/pipeline.json)
_THIS_DIR = Path(__file__).resolve().parent
if str(_THIS_DIR) not in sys.path:
    sys.path.insert(0, str(_THIS_DIR))
from hack_pipeline import (  # noqa: E402
    hack_must_have_either,
    hack_soft_warn,
    phase_for_todo,
    required_skills_for_phase,
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
    tag: str
    msg: str


Decision = Union[Allow, Deny, Block, SoftWarn]


# ───── GuardState ────────────────────────────────────────────────

@dataclass
class GuardState:
    """单任务的全部守卫态. SDK hook 闭包持有此对象."""

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

    # Stop hook 阻断计数 (上限 _MAX_STOP_BLOCKS)
    stop_block_count: int = 0
    stop_bypassed: str | None = None      # 上限后写入, 紫标用

    # Stop hook soft warn (可选 skill 缺失, 由 pipeline.json.stop_hook_soft_warn 声明)
    soft_skipped: list[str] = field(default_factory=list)

    # pause/unpause 控制 (SDK 化后底层简化, 不再 SIGSTOP)
    paused: bool = False


# Stop hook 阻断次数硬上限. 防止 stop_hook_active 协议字段被漏处理时死循环.
_MAX_STOP_BLOCKS = 3


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
                ph = phase_for_todo(
                    td.get("content") or td.get("activeForm") or "",
                    skill_name=state.skill_name,
                )
                if ph is not None:
                    completed.add(ph)
            state.todo_completed_phases = completed


# ───── PreToolUse(TodoWrite): Layer 1 ───────────────────────────

def check_pretool_todowrite(payload: dict, state: GuardState) -> Decision:
    """Layer 1: TodoWrite 把 phase 改 completed 时, 对应 skill 必须出现过.

    启用条件: skill 的 pipeline.json 声明了 phase_required.
    无声明 = 空 dict → 直接放行 (skill-agnostic).
    """
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
        ph = phase_for_todo(td.get("content") or "", skill_name=state.skill_name)
        if ph is None:
            continue
        required_groups = required_skills_for_phase(ph, skill_name=state.skill_name)
        if not required_groups:
            continue
        for group in required_groups:
            if not any(s in called for s in group):
                missing.append((ph, "/".join(group)))

    if not missing:
        return Allow()

    lines = [
        "TodoWrite 阻断: 你试图把以下 phase 标 completed, 但对应 skill 从未通过 Skill 工具调起。",
        "这违反当前 skill 的 pipeline 声明 (~/.claude/skills/<name>/pipeline.json 的 phase_required):",
        "",
        "  > 每个 phase 的 completed 必须有对应 Skill 工具调用记录",
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


# ───── Stop hook: Layer 2 + report.md 落盘 ─────────────────────

def check_stop(payload: dict, state: GuardState) -> Decision:
    """Stop hook: report-file 落盘检查 + Layer 2 必经 skill.

    - stop_hook_active=True: 协议层防循环, 放行
    - stop_block_count >= _MAX_STOP_BLOCKS: 上限放行, 写 state.stop_bypassed
    - report.md 落盘: 通用检查, 只要 Skill(report) 被调过就必须真的写出文件
    - Layer 2 必经 skill: pipeline.json 的 stop_hook_required 声明; 空则跳过
    """
    if payload.get("stop_hook_active"):
        return Allow()

    if state.stop_block_count >= _MAX_STOP_BLOCKS:
        state.stop_bypassed = f"Stop hook bypassed after {state.stop_block_count} blocks"
        return Allow()

    workdir = state.workdir
    called = _skill_name_in_events(state)

    # 通用: report skill 调过但 workdir 没真写出 report 文件 → 阻断
    # 任何 skill 都可能有 report 步骤, 此检查跨 skill 生效.
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

    # Layer 2: 必经 skill (当前 skill 的 pipeline.json.stop_hook_required)
    must_have_either: list[list[str]] = list(hack_must_have_either(skill_name=state.skill_name))
    if not must_have_either:
        # 当前 skill 没声明 stop_hook_required → 无强制要求, 放行
        return Allow()

    missing: list[str] = []
    for opts in must_have_either:
        if not any(s in called for s in opts):
            missing.append(opts[0])

    # 软警告 (当前 skill 的 pipeline.json.stop_hook_soft_warn)
    soft_skipped: list[str] = []
    for opts in hack_soft_warn(skill_name=state.skill_name):
        if not any(s in called for s in opts):
            soft_skipped.append(opts[0])
    if soft_skipped:
        state.soft_skipped = soft_skipped

    if not missing:
        return Allow()

    state.stop_block_count += 1
    return Block(
        f"Stop 阻断 ({state.stop_block_count}/{_MAX_STOP_BLOCKS}): "
        f"当前 skill 声明的必经步骤仍有未调起的 — 不准结束。\n\n"
        "缺失 (优先按此顺序补齐):\n"
        + "\n".join(f"  □ Skill(skill=\"{s}\")" for s in missing)
        + "\n\n"
        "每一个都必须真的调起 (加载对应 SKILL.md 并按其执行), 不许在文本里复述。\n\n"
        "如果当前目标真的不适用整条流水线, 在 report.md 里说明跳过原因, 而不是静默结束."
    )


# ───── PhaseReport (替代 phase_audit.audit) ───────────────────

@dataclass
class PhaseReport:
    """Phase 执行审计结果, runner 收尾根据 violations 决定是否续跑."""
    skills_called: set[str] = field(default_factory=set)
    todo_completed_phases: set[int] = field(default_factory=set)
    violations: list[dict] = field(default_factory=list)
    missing_mandatory: list[str] = field(default_factory=list)


def audit_phases(state: GuardState) -> PhaseReport:
    """收尾复盘: 检查 phase completed 是否有对应 skill 调用记录.

    通用逻辑 (v2.0):
    - 首条 prompt 的 slash command 对应 skill 必须被调过 (适用所有 skill)
    - 若 skill 声明了 phase_required, 每个 completed phase 也要有对应 skill 调用

    历史 (v1.x): 只有 hack skill 才做完整 phase 校验, 其他 skill 只验首条 skill 调用.
    v2.0: 校验规则完全由 pipeline.json 驱动, 不硬绑 skill 名.
    """
    rep = PhaseReport()
    _skill_name_in_events(state)  # 刷新 skills_called
    rep.skills_called = set(state.skills_called)
    rep.todo_completed_phases = set(state.todo_completed_phases)

    skill_name = state.skill_name

    # 首条 prompt 的 slash command 必须被真调过 (通用)
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

    # 每个 completed phase 必须有对应 skill 调用证据 (仅当 skill 声明 phase_required)
    for phase in sorted(rep.todo_completed_phases):
        required_groups = required_skills_for_phase(phase, skill_name=skill_name)
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
                        "这违反 skill 的 pipeline 声明。"
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
    """phase_skip 违规时的纠偏 prompt."""
    lines = [
        f"[Phase 执行审计不达标 / 第 {attempt}/{max_attempts} 次提醒]",
        "",
        "本轮事件流重放显示: 部分 skill / phase 缺少真实的 Skill 工具调用记录。",
        "",
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
