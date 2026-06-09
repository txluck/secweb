"""认证与首页路由 - 登录/登出/静态首页/改密。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
)

from ..deps import (
    SESSION_COOKIE,
    STATIC_DIR,
    is_authed,
    issue_token,
    password_is_default,
    require_auth,
    set_password,
    verify_password,
)

router = APIRouter()


@router.get("/", response_class=HTMLResponse)
async def index(request: Request):
    if not is_authed(request):
        return RedirectResponse("/login")
    # 动态注入 app.js mtime 作 cache-busting 版本号, 避免浏览器加载旧版本
    # (后端代码改了 / 加了新 skill, 前端拿到的还是缓存的老 app.js).
    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    app_js = STATIC_DIR / "app.js"
    if app_js.exists():
        ver = int(app_js.stat().st_mtime)
        html = html.replace('/static/app.js"', f'/static/app.js?v={ver}"')
    return HTMLResponse(html)


@router.get("/login", response_class=HTMLResponse)
async def login_page():
    return FileResponse(STATIC_DIR / "login.html")


@router.post("/login")
async def login_submit(password: str = Form(...)):
    if not verify_password(password):
        return JSONResponse({"ok": False, "error": "密码错误"}, status_code=401)
    resp = JSONResponse({"ok": True})
    resp.set_cookie(
        SESSION_COOKIE, issue_token(),
        max_age=7 * 24 * 3600,
        httponly=True, samesite="lax",
    )
    return resp


@router.post("/logout")
async def logout():
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(SESSION_COOKIE)
    return resp


@router.get("/api/auth/status", dependencies=[Depends(require_auth)])
async def auth_status():
    """登录后 UI 用来知道是否还在用 env 默认密码 (提示用户修改)."""
    return {"password_is_default": password_is_default()}


@router.post("/api/auth/change-password", dependencies=[Depends(require_auth)])
async def change_password(payload: dict):
    current = (payload.get("current_password") or "").strip()
    new = (payload.get("new_password") or "").strip()
    confirm = (payload.get("confirm_password") or "").strip()
    if not current or not new:
        raise HTTPException(400, "current_password / new_password 必填")
    if new != confirm:
        raise HTTPException(400, "两次输入的新密码不一致")
    if len(new) < 6:
        raise HTTPException(400, "新密码至少 6 位")
    if not verify_password(current):
        raise HTTPException(401, "当前密码错误")
    if new == current:
        raise HTTPException(400, "新密码不能与旧密码相同")
    await set_password(new)
    # 颁发新 token, 旧 cookie 仍然有效 (签名相同 secret), 但用户重登更稳妥
    return {"ok": True}
