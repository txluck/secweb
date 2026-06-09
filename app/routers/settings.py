"""应用设置 - 邮件 / 模型 等。"""
from __future__ import annotations

import os

from fastapi import APIRouter, Depends, HTTPException

from .. import store
from ..deps import require_auth
from ..notify import (
    MailConfig,
    get_mail_config,
    save_mail_config,
    send_mail,
)

router = APIRouter(dependencies=[Depends(require_auth)])

# 模型设置 key (app_settings 表)
_MODEL_KEY = "current_model"

# 可选模型清单 — 给前端下拉显示. 实际值由 ANTHROPIC_BASE_URL 端点决定接受哪些 ID,
# 这里给出常见 Claude 4.x 系列 + GPT 5.x 系列 + 默认空(SDK 走 claude_code preset).
# 用户可自己输入任意 ID, 此清单仅做下拉快捷选择.
_MODEL_PRESETS = [
    {"id": "", "label": "默认(走 claude_code preset)"},
    {"id": "claude-opus-4-7[1M]", "label": "Opus 4.7 (1M ctx)"},
    {"id": "claude-opus-4-7", "label": "Opus 4.7"},
    {"id": "claude-opus-4-8", "label": "Opus 4.8"},
    {"id": "claude-sonnet-4-6", "label": "Sonnet 4.6"},
    {"id": "claude-sonnet-4-7", "label": "Sonnet 4.7"},
    {"id": "claude-haiku-4-5", "label": "Haiku 4.5"},
    {"id": "gpt-5.5", "label": "GPT-5.5"},
    {"id": "gpt-5.4", "label": "GPT-5.4"},
]


def get_current_model() -> str:
    """优先级: DB (app_settings.current_model) > env ANTHROPIC_MODEL > 空字符串."""
    rec = store.get_setting(_MODEL_KEY) or {}
    v = rec.get("value", "")
    if isinstance(v, str) and v:
        return v
    return os.environ.get("ANTHROPIC_MODEL", "")


@router.get("/api/model")
async def api_get_model():
    return {
        "current": get_current_model(),
        "presets": _MODEL_PRESETS,
        "env_default": os.environ.get("ANTHROPIC_MODEL", ""),
    }


@router.post("/api/model")
async def api_set_model(payload: dict):
    """切换全局默认模型. 落 app_settings 表, 后续新任务起效.

    已经在跑的任务不会切, 因为 ClaudeSDKClient 启动时已经绑定模型.
    要切已跑任务: stop + retry (sched.retry_task fresh=False).
    """
    model = (payload.get("model") or "").strip()
    # 不强制校验 model id, 因为中转网关支持的 id 列表可能变化
    await store.set_setting(_MODEL_KEY, {"value": model})
    return {"current": get_current_model()}


@router.get("/api/settings/mail")
async def api_get_mail():
    return get_mail_config().to_public_dict()


@router.post("/api/settings/mail")
async def api_save_mail(payload: dict):
    cfg = await save_mail_config(payload)
    return cfg.to_public_dict()


@router.post("/api/settings/mail/test")
async def api_test_mail(payload: dict | None = None):
    cfg = get_mail_config()
    if not cfg.smtp_host or not cfg.from_addr or not cfg.to_addrs:
        raise HTTPException(400, "mail config incomplete (smtp_host / from_addr / to_addrs)")
    # 测试邮件不受 enabled 开关限制, 但也不修改配置
    test_cfg = MailConfig(**{**cfg.__dict__, "enabled": True})
    try:
        await send_mail(
            "[secweb] 测试邮件",
            "这是一封来自 secweb 的测试邮件。如果你收到了, 说明 SMTP 配置正确。",
            test_cfg,
        )
    except Exception as e:
        raise HTTPException(400, f"send failed: {e!r}")
    return {"ok": True}
