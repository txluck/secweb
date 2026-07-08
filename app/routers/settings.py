"""应用设置 - 邮件 / 模型 等。"""
from __future__ import annotations

import os

from fastapi import APIRouter, Depends, HTTPException

from .. import store
from ..config import ROOT, _load_dotenv
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
# TEMP 目录设置 key (app_settings 表)
_TEMP_DIR_KEY = "current_temp_dir"

# .env 文件路径 (config.ROOT 即项目根). 单文件, 不允许路径穿越.
_ENV_FILE = ROOT / ".env"
_ENV_BAK = ROOT / ".env.bak"
# .env 上限 128KB (真实 .env 不会这么大, 防止误传大文件)
_ENV_MAX_SIZE = 128 * 1024


# 热配置 key 白名单: 保存 .env 后即时生效 (get_current_model/get_current_temp_dir
# 走 os.environ fallback). 其他 key (HOST/PORT/PASSWORD 等) 已被 Config.load()
# 缓存在启动时, 需重启才生效, 前端会给对应提示.
_HOT_ENV_KEYS = {
    "ANTHROPIC_BASE_URL",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_MODEL",
    "SECWEB_TEMP_DIR",
}

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


def get_current_temp_dir() -> str:
    """优先级: DB (app_settings.current_temp_dir) > env SECWEB_TEMP_DIR > 空字符串.

    留空表示走系统默认 (%TEMP%/$TMPDIR). 用于 Windows U+2018 等
    unicode 用户名污染 %USERPROFILE% 时切到干净路径.
    """
    rec = store.get_setting(_TEMP_DIR_KEY) or {}
    v = rec.get("value", "")
    if isinstance(v, str) and v:
        return v
    return os.environ.get("SECWEB_TEMP_DIR", "")


@router.get("/api/settings/temp_dir")
async def api_get_temp_dir():
    return {
        "current": get_current_temp_dir(),
        "env_default": os.environ.get("SECWEB_TEMP_DIR", ""),
    }


@router.post("/api/settings/temp_dir")
async def api_set_temp_dir(payload: dict):
    """设置子进程 TEMP 目录. 新任务生效, 在跑任务不切.

    留空 = 清空 DB 覆盖, 回退到 env 或系统默认.
    """
    from pathlib import Path
    temp_dir = (payload.get("temp_dir") or "").strip()
    if temp_dir:
        # 尝试创建目录 (若不存在), 校验可写. 失败直接 400 而不是运行时炸.
        try:
            p = Path(temp_dir).expanduser()
            p.mkdir(parents=True, exist_ok=True)
            if not p.is_dir():
                raise ValueError("not a directory")
        except Exception as e:
            raise HTTPException(400, f"invalid temp_dir: {e!r}")
        temp_dir = str(p)
    await store.set_setting(_TEMP_DIR_KEY, {"value": temp_dir})
    return {"current": get_current_temp_dir()}


def _parse_env_keys(text: str) -> set[str]:
    """从 .env 文本抽出 KEY 集合, 用来判断哪些改动是热配置."""
    keys: set[str] = set()
    for line in text.splitlines():
        s = line.strip()
        if not s or s.startswith("#") or "=" not in s:
            continue
        k = s.split("=", 1)[0].strip()
        if k:
            keys.add(k)
    return keys


@router.get("/api/settings/env")
async def api_get_env():
    """读 ROOT/.env 原始文本. 不存在返回空串.

    注意: 内容包含 ANTHROPIC_AUTH_TOKEN 等敏感信息, 但接口已被
    router-level require_auth 保护 (单租户模型, 登录用户 = 管理员).
    """
    content = ""
    if _ENV_FILE.exists():
        try:
            content = _ENV_FILE.read_text(encoding="utf-8")
        except Exception as e:
            raise HTTPException(500, f"read .env failed: {e!r}")
    return {
        "content": content,
        "path": str(_ENV_FILE),
        "exists": _ENV_FILE.exists(),
        "hot_keys": sorted(_HOT_ENV_KEYS),
    }


@router.post("/api/settings/env")
async def api_set_env(payload: dict):
    """整体覆写 ROOT/.env. 写入前先备份到 .env.bak.

    保存后立刻调用 _load_dotenv() 刷 os.environ, 使**热配置**即时生效
    (ANTHROPIC_* / SECWEB_TEMP_DIR — 走 get_current_* 的 env fallback).

    冷配置 (HOST/PORT/PASSWORD/DEFAULT_CONCURRENCY/CLAUDE_BIN 等) 已被
    Config.load() 缓存在启动时, 需重启服务才生效. 响应体的 restart_required
    字段告诉前端哪些改动需要重启, 前端据此提示用户.
    """
    content = payload.get("content", "")
    if not isinstance(content, str):
        raise HTTPException(400, "content must be a string")
    if len(content.encode("utf-8")) > _ENV_MAX_SIZE:
        raise HTTPException(400, f"content too large (>{_ENV_MAX_SIZE} bytes)")

    # 读旧内容对比, 挑出变化的 key
    old_content = ""
    if _ENV_FILE.exists():
        try:
            old_content = _ENV_FILE.read_text(encoding="utf-8")
        except Exception:
            old_content = ""
    old_map = _kv_map(old_content)
    new_map = _kv_map(content)
    changed_keys: set[str] = set()
    for k in set(old_map) | set(new_map):
        if old_map.get(k) != new_map.get(k):
            changed_keys.add(k)

    # 备份旧文件 (即使只有一份也好过没有)
    if _ENV_FILE.exists():
        try:
            _ENV_BAK.write_text(old_content, encoding="utf-8")
        except Exception:
            pass  # 备份失败不阻塞写入

    # 写入
    try:
        _ENV_FILE.write_text(content, encoding="utf-8")
    except Exception as e:
        raise HTTPException(500, f"write .env failed: {e!r}")

    # 刷新 os.environ (热配置立即生效; 冷配置 Config 实例已固化, 无效)
    try:
        _load_dotenv()
    except Exception:
        pass

    restart_required = sorted(k for k in changed_keys if k not in _HOT_ENV_KEYS)
    hot_applied = sorted(k for k in changed_keys if k in _HOT_ENV_KEYS)
    return {
        "content": content,
        "path": str(_ENV_FILE),
        "backup": str(_ENV_BAK) if _ENV_BAK.exists() else "",
        "hot_applied": hot_applied,
        "restart_required": restart_required,
    }


def _kv_map(text: str) -> dict[str, str]:
    """把 .env 文本 parse 成 {KEY: value} 字典 (行内注释按 dotenv 语义忽略).

    简化版: 只处理 KEY=VALUE 形式, 不支持引号内的等号 / 多行值.
    足够检测 key 增删改.
    """
    out: dict[str, str] = {}
    for line in text.splitlines():
        s = line.strip()
        if not s or s.startswith("#") or "=" not in s:
            continue
        k, v = s.split("=", 1)
        k = k.strip()
        if not k:  # "=x" 这类无效行, 跳过 (与 _load_dotenv 保持一致行为)
            continue
        out[k] = v.strip()
    return out


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
