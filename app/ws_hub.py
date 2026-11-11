"""WebSocket hub — the single server->client push channel.

Threading rule: async tasks (simulation, sentinel) call `await hub.broadcast()`
directly; sync code running in worker threads (the chat pipeline, tool bridge,
metrics recording) must only use `hub.broadcast_threadsafe()`.
"""

import asyncio
import json
import logging
from datetime import datetime, timezone

logger = logging.getLogger(__name__)


class WSHub:
    def __init__(self):
        self._clients = set()
        self._loop = None

    def set_loop(self, loop) -> None:
        self._loop = loop

    async def connect(self, ws) -> None:
        await ws.accept()
        self._clients.add(ws)

    def disconnect(self, ws) -> None:
        self._clients.discard(ws)

    @property
    def client_count(self) -> int:
        return len(self._clients)

    async def broadcast(self, msg_type: str, data: dict) -> None:
        payload = json.dumps({
            "type": msg_type,
            "ts": datetime.now(timezone.utc).isoformat(),
            "data": data,
        })
        for ws in list(self._clients):
            try:
                await ws.send_text(payload)
            except Exception:
                self._clients.discard(ws)

    def broadcast_threadsafe(self, msg_type: str, data: dict) -> None:
        if self._loop is None:
            return
        asyncio.run_coroutine_threadsafe(self.broadcast(msg_type, data), self._loop)


hub = WSHub()
