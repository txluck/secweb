# Skill Author Guide

如何让你的 Claude Code skill 跟 secweb dashboard 配合 — 让 dashboard 的 hook 守卫识别你的 skill, 强制流水线, 防漏测.

## 30 秒上手

把你的 skill 放在 `~/.claude/skills/<name>/`. dashboard 启动时会扫描每个目录, 找 `pipeline.json`. **找不到就用内置默认值, 完全不影响**.

最小可用 `pipeline.json`:

```json
{
  "command_to_skill": {
    "your-skill": "your-skill"
  }
}
```

这样模型用 `/your-skill <url>` 时, dashboard 会识别这是你的 skill (通过 SECWEB_SKILL_NAME env 注入到 hook), 后续所有守卫层就都生效了.

## 完整配置 schema

每个字段都是可选, 缺哪个 fallback 哪个的内置默认值.

```json
{
  "phase_required": {
    "0": [["recon"]],
    "1": [["js-audit", "miniprogram-audit"]],
    "2": [["auth-bypass"]],
    "3": [["sqli"]],
    "4": [["business-logic"]],
    "5": [["validate"], ["report"]]
  },
  "phase_keywords": {
    "0": ["phase 0", "phase0", "侦察", "recon"],
    "1": ["phase 1", "phase1", "js审计", "js-audit"]
  },
  "stop_hook_required": [
    ["recon"],
    ["js-audit", "miniprogram-audit"],
    ["auth-bypass"],
    ["sqli"],
    ["business-logic"],
    ["validate"],
    ["report"]
  ],
  "stop_hook_soft_warn": [
    ["xss"],
    ["open-redirect"]
  ],
  "command_to_skill": {
    "your-skill": "your-skill"
  },
  "shallow_ok_skills_extra": [],
  "fuzz_families": {
    "your-injection-skill": {
      "min_families": 4,
      "families": {
        "family_a": ["payload1", "payload2"],
        "family_b": ["payload3"]
      }
    }
  }
}
```

## 字段语义

| 字段 | 类型 | 含义 |
|---|---|---|
| `phase_required` | `{phase_id: list[list[str]]}` | 用户的 todo 标 "Phase N completed" 时, 这些 skill 必须有对应 Skill 调用. 内层 list 表示 "任一即可" (`["a", "b"]` = a 或 b 调过就行). 外层 list 表示 "全部必须" |
| `phase_keywords` | `{phase_id: list[str]}` | TodoWrite 内容包含哪些关键词时识别为该 phase |
| `stop_hook_required` | `list[list[str]]` | Stop hook 时强制必经 (缺则拒退出, hard block) |
| `stop_hook_soft_warn` | `list[list[str]]` | Stop 时建议但不强制 (缺则紫标提醒, soft warn) |
| `command_to_skill` | `{cmd: skill_name}` | slash command → skill 目录名映射. 用户输入 `/cmd` 时 dashboard 知道是哪个 skill |
| `shallow_ok_skills_extra` | `list[str]` | 追加到"辅助型 skill"清单 (Layer 3 不查这些 skill 的执行深度). 默认已含 retrospective/secknowledge/report |
| `fuzz_families` | `{skill: {min_families, families}}` | 自定义 fuzz 家族表, 检测 sub-skill 时段是否覆盖 ≥N 种 payload 模式 |

## 多套件 merge 规则

如果用户同时装了多个 skill 套件 (例如 hack + redteam), dashboard 会**合并**所有 pipeline.json:

| 字段 | 合并策略 |
|---|---|
| `phase_required` | 取并集 (最严格 — 任一套件要求都要满足) |
| `phase_keywords` | 取并集 |
| `stop_hook_required` | 取并集 |
| `stop_hook_soft_warn` | 取并集 |
| `command_to_skill` | 后加载者覆盖 |
| `shallow_ok_skills_extra` | 取并集 (用户不能"减少"豁免, 防降级安全) |
| `fuzz_families[skill].min_families` | 取最大值 |
| `fuzz_families[skill].families` | 取并集 |

合并后默认值兜底每个字段.

## 完整示例 (以 hack 流水线为参考)

参考 `~/.claude/skills/hack/pipeline.json` (dashboard 会自动导出一份默认).

## 常见用例

### 用例 1: 新增一个不在主流水线的 skill

只加 `command_to_skill`:
```json
{"command_to_skill": {"my-tool": "my-tool"}}
```
模型用 `/my-tool <url>` 时 dashboard 知道是哪个 skill, 但不会强制必经.

### 用例 2: 加新的 fuzz skill (例如 graphql)

```json
{
  "command_to_skill": {"graphql-audit": "graphql-audit"},
  "fuzz_families": {
    "graphql-audit": {
      "min_families": 3,
      "families": {
        "introspection": ["__schema", "__type", "__typename"],
        "field_suggest": ["query{__schema{types{name}}}", "did you mean"],
        "depth_attack": ["{user{posts{user{posts"]
      }
    }
  }
}
```

模型调用 `Skill(graphql-audit)` 时, dashboard Layer 4 会校验时段内是否出现 ≥3 类 graphql payload.

### 用例 3: 把某 skill 改成必经

```json
{
  "stop_hook_required": [
    ["recon"],
    ["js-audit", "miniprogram-audit"],
    ["auth-bypass"],
    ["sqli"],
    ["graphql-audit"],
    ["business-logic"],
    ["validate"],
    ["report"]
  ]
}
```

用户的任务结束前必须调用过 graphql-audit, 否则 Stop hook 拒退出.

### 用例 4: 只想用 dashboard 但不要任何强制

```json
{
  "stop_hook_required": [],
  "stop_hook_soft_warn": [],
  "phase_required": {}
}
```

完全软模式. 工具事件仍记录, 但没有 hard block.

## 调试

dashboard 启动时, `skill_pipeline_loader` 会扫一次配置. 看是否被识别:

```bash
.venv/bin/python -c "from app.skill_pipeline_loader import get_config; \
import json; c=get_config(); \
print('source:', c['_source']); \
print('skills:', c['_skills_loaded'])"
```

如果你的 skill 名没在 `skills` 里出现, 说明 `pipeline.json` 没被加载. 可能原因:
- 文件在错误的目录 (必须是 `~/.claude/skills/<name>/pipeline.json`)
- JSON 语法错误 (启动时 stderr 会有 parse error)

## 安全注意

- `shallow_ok_skills_extra` 只能追加豁免, 不能从默认清单删除. 防止用户绕过 dashboard 的 Layer 3 深度检测.
- `phase_required` 多套件取并集, 用户不能通过加新套件"放宽"已有约束.
- 改 `pipeline.json` 后需要重启 dashboard 才生效 (启动时缓存).
