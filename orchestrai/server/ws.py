"""WebSocket connection manager.

Maintains a set of active client connections and broadcasts events to all of
them. Push-only — the API server never accepts commands over WS.
"""

import asyncio
import json
from typing import Any

from fastapi import WebSocket


class WSManager:
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

    async def broadcast(self, frame: dict[str, Any]) -> None:
        payload = json.dumps(frame, separators=(",", ":"))
        async with self._lock:
            dead: list[WebSocket] = []
            for ws in list(self._clients):
                try:
                    await ws.send_text(payload)
                except Exception:
                    dead.append(ws)
            for ws in dead:
                self._clients.discard(ws)


manager = WSManager()
