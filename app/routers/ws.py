"""WebSocket 实时事件推送路由。"""
from __future__ import annotations

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from ..deps import BROADCAST, SESSION_COOKIE, verify_ws_cookie

router = APIRouter()


@router.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    cookie = ws.cookies.get(SESSION_COOKIE)
    if not verify_ws_cookie(cookie):
        await ws.close(code=4401)
        return
    await BROADCAST.connect(ws)
    try:
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        await BROADCAST.disconnect(ws)
