"""Skill pipeline 配置加载器 — 让用户只需放 pipeline.json 即可自定义 dashboard 行为.

设计目标 (v2.0 skill-agnostic):
- secweb 是通用调度器, 不假设任何 skill 名或流水线
- 有 pipeline.json 的 skill (如 hack) → 加载其流水线, 走完整守卫
- 无 pipeline.json 的 skill (如 prowl / 用户自建) → 走"零守卫轻量模式",
  完全按 skill 自己的 SKILL.md / hooks 执行, secweb 只做通用底线 (授权 + 报告 + 中文)
- 多套件可共存, merge 时取并集

加载策略:
1. 扫 ~/.claude/skills/<skill_name>/pipeline.json
2. 找到的全部 merge 到一份运行时配置
3. 没找到任何文件 → 空配置 (无 phase / 无 stop_required / 无 command_map),
   动态兜底靠 skill_contract.detect_slash_skill 扫 SKILL.md 存在性

JSON schema (示例见 ~/.claude/skills/hack/pipeline.json):
{
  "phase_required": {"0": [["recon"]], "1": [["js-audit", "miniprogram-audit"]], ...},
  "phase_keywords": {"0": ["phase 0", "侦察", ...], ...},
  "stop_hook_required": [["recon"], ["js-audit", "miniprogram-audit"], ...],
  "stop_hook_soft_warn": [["xss"], ["open-redirect"]],
  "command_to_skill": {"hack": "hack", "recon": "recon", ...},
  "fuzz_families": {"sqli": {"min_families": 5, "families": {...}}, ...},
  "shallow_ok_skills": ["retrospective", "secknowledge", "report"]
}
所有字段都是可选, 缺哪个就走空默认.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

# ───── 内置默认值 (v2.0: 空 — skill-agnostic) ────────────────────
# secweb 不再假设任何 skill 名或流水线.
# 有 pipeline.json 的 skill (如 hack) → 加载其配置, 走完整守卫
# 无 pipeline.json 的 skill → 空配置, secweb 只做通用底线 (report.md 落盘等)
# 历史 (v1.x): 这里硬编码 hack 6 阶段流水线, 导致所有非 hack skill 被强套 hack 检查.

_DEFAULT_PHASE_REQUIRED: dict[int, list[list[str]]] = {}
_DEFAULT_PHASE_KEYWORDS: dict[int, tuple[str, ...]] = {}
_DEFAULT_STOP_REQUIRED: list[list[str]] = []
_DEFAULT_SOFT_WARN: list[list[str]] = []
_DEFAULT_COMMAND_TO_SKILL: dict[str, str] = {}

_DEFAULT_SHALLOW_OK_SKILLS: set[str] = {"retrospective", "secknowledge", "report"}

# Layer 6 retrospective 自查关键词. retrospective skill 内容里出现任一即视为
# "做了漏测自查". 默认涵盖中英文两套术语 (hack/SKILL.md 用中文, 第三方 skill
# 可能用英文). 用户可在 pipeline.json 加 "retrospective_keywords_extra"
# 追加自己 skill 的术语 — 取并集 (用户不能删除默认, 防降级).
_DEFAULT_RETRO_KEYWORDS: list[str] = [
    "横向扩展", "首漏扩展", "未测端点", "漏测",
    "覆盖率", "铁律 6", "铁律6",
    "lateral", "blind spot", "missed",
    "post-exploitation", "chain attack", "privilege escalation",
    "untested", "skipped",
    # 路径组合 / CRUD 全覆盖类 — 防漏测兄弟接口 (找到一个端点后, 同前缀
    # 的其他 CRUD 动词如果没扫, 就会漏掉同一类越权)
    "路径组合", "前缀组合", "CRUD 全覆盖", "CRUD全覆盖",
    "兄弟接口", "兄弟端点", "同模型方法",
    "path combination", "sibling endpoints", "CRUD coverage",
]

# fuzz 家族表: 大且固定, 让 pretool_guard 自己保留默认, 这里只暴露 override 入口
# 用户在 pipeline.json 写 "fuzz_families" 字段时会覆盖对应 skill 的家族表

# ───── 缓存 + 加载 ───────────────────────────────────────────

_SKILLS_ROOT = Path.home() / ".claude" / "skills"
_PIPELINE_FILE_NAME = "pipeline.json"

# 进程级缓存. 启动后扫一次, 后续 hook 调用 0 IO.
_cache: dict[str, Any] | None = None


def _read_one(skill_dir: Path) -> dict | None:
    """读单个 skill 的 pipeline.json, 解析失败返回 None (不阻断启动)."""
    f = skill_dir / _PIPELINE_FILE_NAME
    if not f.is_file():
        return None
    try:
        return json.loads(f.read_text(encoding="utf-8"))
    except Exception as e:
        # 启动期解析错误打到 stderr, 但不抛 (保证 fallback 路径可用)
        import sys
        sys.stderr.write(
            f"[skill_pipeline_loader] {f} parse error: {e}, "
            f"falling back to defaults\n"
        )
        return None


def _scan_user_configs() -> list[tuple[str, dict]]:
    """扫 ~/.claude/skills/*/pipeline.json. 返回 [(skill_name, config), ...]."""
    out: list[tuple[str, dict]] = []
    if not _SKILLS_ROOT.is_dir():
        return out
    for entry in sorted(_SKILLS_ROOT.iterdir()):
        if not entry.is_dir():
            continue
        cfg = _read_one(entry)
        if cfg is not None:
            out.append((entry.name, cfg))
    return out


def _merge_phase_required(configs: list[tuple[str, dict]]) -> dict[int, list[list[str]]]:
    """多套件合并 phase_required. 取并集 (最严格)."""
    merged: dict[int, list[list[str]]] = {}
    for _, cfg in configs:
        pr = cfg.get("phase_required") or {}
        for k, v in pr.items():
            try:
                phase = int(k)
            except Exception:
                continue
            if not isinstance(v, list):
                continue
            existing = merged.setdefault(phase, [])
            for group in v:
                if isinstance(group, list) and group not in existing:
                    existing.append(group)
    return merged


def _merge_phase_keywords(configs: list[tuple[str, dict]]) -> dict[int, tuple[str, ...]]:
    """多套件合并 phase_keywords. 取并集去重."""
    merged: dict[int, set[str]] = {}
    for _, cfg in configs:
        pk = cfg.get("phase_keywords") or {}
        for k, v in pk.items():
            try:
                phase = int(k)
            except Exception:
                continue
            if not isinstance(v, list):
                continue
            merged.setdefault(phase, set()).update(str(x) for x in v)
    return {k: tuple(sorted(v)) for k, v in merged.items()}


def _merge_skill_list(configs: list[tuple[str, dict]], key: str) -> list[list[str]]:
    """合并 stop_hook_required / stop_hook_soft_warn 这种 list[list[str]] 字段."""
    merged: list[list[str]] = []
    for _, cfg in configs:
        items = cfg.get(key) or []
        for group in items:
            if isinstance(group, list) and group not in merged:
                merged.append(group)
    return merged


def _merge_command_map(configs: list[tuple[str, dict]]) -> dict[str, str]:
    """合并 command_to_skill 字典."""
    merged: dict[str, str] = {}
    for _, cfg in configs:
        m = cfg.get("command_to_skill") or {}
        if isinstance(m, dict):
            for k, v in m.items():
                merged[str(k)] = str(v)
    return merged


def _merge_fuzz_families(configs: list[tuple[str, dict]]) -> dict[str, dict]:
    """合并 fuzz_families. 同 skill 的 families 取合并, min_families 取最大."""
    merged: dict[str, dict] = {}
    for _, cfg in configs:
        ff = cfg.get("fuzz_families") or {}
        if not isinstance(ff, dict):
            continue
        for skill, scfg in ff.items():
            if not isinstance(scfg, dict):
                continue
            target = merged.setdefault(skill, {"min_families": 0, "families": {}})
            mf = scfg.get("min_families")
            if isinstance(mf, int) and mf > target["min_families"]:
                target["min_families"] = mf
            for fname, sigs in (scfg.get("families") or {}).items():
                if isinstance(sigs, list):
                    target["families"][fname] = list(sigs)
            # 透传 idor 类的 min_unique_ids / id_pattern
            for k in ("min_unique_ids", "id_pattern"):
                if k in scfg:
                    target[k] = scfg[k]
    return merged


def _build() -> dict[str, Any]:
    """整合用户配置 → 完整运行时配置.

    v2.0 skill-agnostic:
    - 默认全空. 无 pipeline.json = 零守卫轻量模式.
    - 保留合并视图 (phase_required / stop_hook_required 等) 用于命令映射等通用需求
    - 新增 per_skill dict, Layer 1/2 可用 state.skill_name 查自己 skill 的规则,
      避免非当前 skill 的规则误伤 (如 prowl 任务命中 hack 的 phase_keywords)
    """
    configs = _scan_user_configs()

    # 无任何 pipeline.json → 全空默认
    if not configs:
        return {
            "phase_required": {},
            "phase_keywords": {},
            "stop_hook_required": [],
            "stop_hook_soft_warn": [],
            "command_to_skill": {},
            "shallow_ok_skills": set(_DEFAULT_SHALLOW_OK_SKILLS),
            "retrospective_keywords": set(_DEFAULT_RETRO_KEYWORDS),
            "fuzz_families_override": {},
            "per_skill": {},
            "_source": "defaults",
            "_skills_loaded": [],
        }

    # 有用户配置 → 合并 (用于 command_map 等通用查询)
    phase_required = _merge_phase_required(configs)
    phase_keywords = _merge_phase_keywords(configs)
    stop_required = _merge_skill_list(configs, "stop_hook_required")
    soft_warn = _merge_skill_list(configs, "stop_hook_soft_warn")
    cmd_map = _merge_command_map(configs)
    fuzz_override = _merge_fuzz_families(configs)

    # shallow_ok_skills 合并: 默认 + 用户追加
    shallow_ok = set(_DEFAULT_SHALLOW_OK_SKILLS)
    for _, cfg in configs:
        extra = cfg.get("shallow_ok_skills_extra") or []
        if isinstance(extra, list):
            shallow_ok.update(str(x) for x in extra)

    # retrospective 关键词合并
    retro_kws = set(_DEFAULT_RETRO_KEYWORDS)
    for _, cfg in configs:
        extra = cfg.get("retrospective_keywords_extra") or []
        if isinstance(extra, list):
            retro_kws.update(str(x) for x in extra)

    # per-skill 视图: {skill_name: {phase_required, phase_keywords, stop_hook_required, ...}}
    # Layer 1/2 用当前 skill_name 查自己的规则, 避免跨 skill 污染
    per_skill: dict[str, dict[str, Any]] = {}
    for skill_name, cfg in configs:
        skill_view: dict[str, Any] = {}
        # phase_required: {int: [[str,...]]}
        pr = cfg.get("phase_required") or {}
        skill_pr: dict[int, list[list[str]]] = {}
        for k, v in pr.items():
            try:
                phase = int(k)
            except Exception:
                continue
            if isinstance(v, list):
                skill_pr[phase] = [g for g in v if isinstance(g, list)]
        if skill_pr:
            skill_view["phase_required"] = skill_pr
        # phase_keywords: {int: (str,...)}
        pk = cfg.get("phase_keywords") or {}
        skill_pk: dict[int, tuple[str, ...]] = {}
        for k, v in pk.items():
            try:
                phase = int(k)
            except Exception:
                continue
            if isinstance(v, list):
                skill_pk[phase] = tuple(sorted(str(x) for x in v))
        if skill_pk:
            skill_view["phase_keywords"] = skill_pk
        # stop_hook_required
        sr = cfg.get("stop_hook_required") or []
        skill_sr = [g for g in sr if isinstance(g, list)]
        if skill_sr:
            skill_view["stop_hook_required"] = skill_sr
        # stop_hook_soft_warn
        sw = cfg.get("stop_hook_soft_warn") or []
        skill_sw = [g for g in sw if isinstance(g, list)]
        if skill_sw:
            skill_view["stop_hook_soft_warn"] = skill_sw
        if skill_view:
            per_skill[skill_name] = skill_view

    return {
        "phase_required": phase_required,
        "phase_keywords": phase_keywords,
        "stop_hook_required": stop_required,
        "stop_hook_soft_warn": soft_warn,
        "command_to_skill": cmd_map,
        "shallow_ok_skills": shallow_ok,
        "retrospective_keywords": retro_kws,
        "fuzz_families_override": fuzz_override,
        "per_skill": per_skill,
        "_source": "user+defaults",
        "_skills_loaded": [name for name, _ in configs],
    }


def get_config() -> dict[str, Any]:
    """获取运行时配置 (启动时构建一次, 后续返回缓存)."""
    global _cache
    if _cache is None:
        _cache = _build()
    return _cache


def reload() -> dict[str, Any]:
    """强制重新加载 (测试用 / 用户手动改 pipeline.json 后可调)."""
    global _cache
    _cache = None
    return get_config()


# ───── 公开接口 (其他模块用这些, 不直接读 _cache) ─────────────────
# v2.0: 传 skill_name 走 per-skill 查询, 避免跨 skill 规则污染

def required_skills_for_phase(phase: int, skill_name: str | None = None) -> list[list[str]]:
    """指定 skill_name → 只查该 skill 的 phase_required; 不指定 → 全局合并."""
    if skill_name:
        ps = get_config().get("per_skill", {}).get(skill_name, {})
        return list(ps.get("phase_required", {}).get(phase, []))
    return list(get_config()["phase_required"].get(phase, []))


def phase_for_todo(content: str, skill_name: str | None = None) -> int | None:
    """指定 skill_name → 只用该 skill 的 phase_keywords; 不指定 → 全局合并."""
    if not content:
        return None
    s = content.lower()
    if skill_name:
        keywords = get_config().get("per_skill", {}).get(skill_name, {}).get("phase_keywords", {})
    else:
        keywords = get_config()["phase_keywords"]
    for phase, keys in keywords.items():
        for k in keys:
            if k in s:
                return phase
    return None


def hack_must_have_either(skill_name: str | None = None) -> list[list[str]]:
    """指定 skill_name → 只查该 skill 的 stop_hook_required; 不指定 → 全局合并.
    函数名 hack_* 是 legacy, 语义已通用化 (读 pipeline.json.stop_hook_required)."""
    if skill_name:
        ps = get_config().get("per_skill", {}).get(skill_name, {})
        return [list(g) for g in ps.get("stop_hook_required", [])]
    return list(get_config()["stop_hook_required"])


def hack_soft_warn(skill_name: str | None = None) -> list[list[str]]:
    """同上, 但取 stop_hook_soft_warn."""
    if skill_name:
        ps = get_config().get("per_skill", {}).get(skill_name, {})
        return [list(g) for g in ps.get("stop_hook_soft_warn", [])]
    return list(get_config()["stop_hook_soft_warn"])


def command_to_skill_map() -> dict[str, str]:
    return dict(get_config()["command_to_skill"])


def shallow_ok_skills() -> set[str]:
    return set(get_config()["shallow_ok_skills"])


def fuzz_families_override() -> dict[str, dict]:
    """返回用户对 fuzz 家族的 override (空 dict 表示完全用 pretool_guard 默认)."""
    return dict(get_config().get("fuzz_families_override", {}))


def retro_keywords() -> set[str]:
    """返回 retrospective 自查关键词集合 (默认 + 用户 retrospective_keywords_extra 并集)."""
    return set(get_config().get("retrospective_keywords", _DEFAULT_RETRO_KEYWORDS))
