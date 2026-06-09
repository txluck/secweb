"""共享依赖与工具 - 各 router 共用的鉴权、Scheduler 取用、配置常量等。

把 cookie 鉴权、Scheduler 单例、Broadcaster 等装配在这里, 路由文件只 import 用。
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from pathlib import Path

from fastapi import HTTPException, Request
from itsdangerous import BadSignature, URLSafeTimedSerializer

from . import store
from .config import Config
from .scheduler import Scheduler
from .ws import Broadcaster

CFG = Config.load()
SIGNER = URLSafeTimedSerializer(CFG.secret_key, salt="secweb-auth")
SESSION_COOKIE = "secweb_session"
SESSION_TTL = 7 * 24 * 3600

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"

# 全局单例: lifespan 里 set, 路由里 get
BROADCAST = Broadcaster()
SCHEDULER: Scheduler | None = None

# ---------- 密码: DB 优先, 回落到 env CFG.password ----------
# DB 中存 PBKDF2-SHA256 哈希 (salt 随机), 避免明文。
_PWD_KEY = "auth"
_PBKDF2_ITERS = 200_000


def _hash_password(password: str, salt: bytes) -> str:
    h = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, _PBKDF2_ITERS)
    return h.hex()


def verify_password(password: str) -> bool:
    """DB 中存了哈希则用哈希校验; 否则回退到 env 明文 (首次启动)."""
    rec = store.get_setting(_PWD_KEY)
    if rec and rec.get("hash") and rec.get("salt"):
        try:
            salt = bytes.fromhex(rec["salt"])
        except ValueError:
            return False
        expected = rec["hash"]
        actual = _hash_password(password, salt)
        return hmac.compare_digest(expected, actual)
    # 没设置过 -> 用 env 明文
    return hmac.compare_digest(password, CFG.password)


async def set_password(new_password: str) -> None:
    salt = secrets.token_bytes(16)
    await store.set_setting(_PWD_KEY, {
        "hash": _hash_password(new_password, salt),
        "salt": salt.hex(),
        "algo": "pbkdf2-sha256",
        "iters": _PBKDF2_ITERS,
    })


def password_is_default() -> bool:
    """密码是否仍是 env 默认值 (尚未在 UI 修改过)."""
    return store.get_setting(_PWD_KEY) is None


def set_scheduler(s: Scheduler) -> None:
    global SCHEDULER
    SCHEDULER = s


def get_scheduler() -> Scheduler:
    assert SCHEDULER is not None, "scheduler not initialized"
    return SCHEDULER


def is_authed(request: Request) -> bool:
    tok = request.cookies.get(SESSION_COOKIE)
    if not tok:
        return False
    try:
        SIGNER.loads(tok, max_age=SESSION_TTL)
        return True
    except BadSignature:
        return False


def require_auth(request: Request) -> None:
    if not is_authed(request):
        raise HTTPException(status_code=401, detail="unauthorized")


def issue_token() -> str:
    return SIGNER.dumps({"t": time.time()})


def verify_ws_cookie(cookie: str | None) -> bool:
    if not cookie:
        return False
    try:
        SIGNER.loads(cookie, max_age=SESSION_TTL)
        return True
    except BadSignature:
        return False
