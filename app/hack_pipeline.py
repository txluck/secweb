"""hack 流水线必经 skill 与 phase 关键词的薄外观层.

历史沿革:
- v0: 散落在 pretool_guard / phase_audit / handle_stop 三处硬编码
- v1.0: 抽到本文件硬编码 (单一真理来源)
- v1.1: 改造为 skill_pipeline_loader 的薄外观, 用户可放
        ~/.claude/skills/<name>/pipeline.json 自定义而无需改代码

零配置兼容性: 没找到任何 pipeline.json 时, loader 用内置默认值,
行为完全等同 v1.0. 现有用户升级零影响.
"""
from __future__ import annotations

import sys
from pathlib import Path

# 兼容 hook 脚本独立调用 (无包上下文) + dashboard 模块导入两种场景
# 跟 pretool_guard.py 的 sys.path 注入是同一原理
_THIS_DIR = Path(__file__).resolve().parent
if str(_THIS_DIR) not in sys.path:
    sys.path.insert(0, str(_THIS_DIR))

from skill_pipeline_loader import (  # noqa: E402
    command_to_skill_map,
    fuzz_families_override,
    hack_must_have_either,
    hack_soft_warn,
    phase_for_todo,
    required_skills_for_phase,
    retro_keywords,
    shallow_ok_skills,
)


# HACK_MUST_HAVE_EITHER / HACK_SOFT_WARN 老接口仍可用 (调用函数)
HACK_MUST_HAVE_EITHER = hack_must_have_either()
HACK_SOFT_WARN = hack_soft_warn()


__all__ = [
    "HACK_MUST_HAVE_EITHER",
    "HACK_SOFT_WARN",
    "command_to_skill_map",
    "fuzz_families_override",
    "hack_must_have_either",
    "hack_soft_warn",
    "phase_for_todo",
    "required_skills_for_phase",
    "retro_keywords",
    "shallow_ok_skills",
]
