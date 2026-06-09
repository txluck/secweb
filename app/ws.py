"""WebSocket 连接管理 - 把执行器事件实时广播给所有已认证客户端。"""
from __future__ import annotations

import asyncio
import json
from typing import Any

from fastapi import WebSocket


class Broadcaster:
    def __init__(self) -> None:
        self._clients: set[WebSocket] = set()
        self._lock = asyncio.Lock()

    async def connect(self, ws: WebSocket) -> None:
        await ws.accept()
        async with self._lock:
            self._clients.add(ws)

    async def disconnect(self, ws: WebSocket) -> None:
        async with self._lock:
            self._clients.discard(ws)

    async def broadcast(self, msg: dict[str, Any]) -> None:
        """并发推送给所有客户端, 慢客户端不阻塞其他人.

        历史 bug: 串行 send_text + 全局锁, 单个慢客户端导致所有人实时日志卡顿.
        现版用 asyncio.gather 并发, 锁仅保护客户端集合的快照, 不锁推送.
        """
        data = json.dumps(msg, ensure_ascii=False, default=str)
        async with self._lock:
            clients_snapshot = list(self._clients)
        if not clients_snapshot:
            return

        async def _send(ws: WebSocket) -> WebSocket | None:
            try:
                await ws.send_text(data)
                return None
            except Exception:
                return ws

        results = await asyncio.gather(
            *(_send(ws) for ws in clients_snapshot), return_exceptions=False
        )
        dead = [ws for ws in results if ws is not None]
        if dead:
            async with self._lock:
                for ws in dead:
                    self._clients.discard(ws)

    async def event_cb(
        self, task_id: str, kind: str, payload: str | dict
    ) -> None:
        await self.broadcast(
            {"type": "event", "task_id": task_id, "kind": kind, "payload": payload}
        )
