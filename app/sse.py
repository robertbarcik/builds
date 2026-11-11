"""Server-sent events: one bounded queue per connected browser.

Messages are `data: {"type": ..., ...}\\n\\n` lines, read by the browser with fetch + a stream
reader (no EventSource), the same pattern as localdesk. Every payload is checked for a
`truth` key before it is queued: ground truth must never reach the browser.
"""
from __future__ import annotations

import asyncio
import json
from typing import Any


class TruthLeak(RuntimeError):
    pass


def assert_no_truth(obj: Any) -> None:
    if isinstance(obj, dict):
        if "truth" in obj:
            raise TruthLeak("payload contains a 'truth' key")
        for v in obj.values():
            assert_no_truth(v)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            assert_no_truth(v)


def format_sse(msg_type: str, data: dict[str, Any]) -> str:
    payload = {"type": msg_type, **data}
    assert_no_truth(payload)
    return f"data: {json.dumps(payload, separators=(',', ':'))}\n\n"


class Hub:
    def __init__(self, maxsize: int = 400):
        self._clients: set[asyncio.Queue[str]] = set()
        self._maxsize = maxsize

    @property
    def client_count(self) -> int:
        return len(self._clients)

    def subscribe(self) -> asyncio.Queue[str]:
        q: asyncio.Queue[str] = asyncio.Queue(maxsize=self._maxsize)
        self._clients.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue[str]) -> None:
        self._clients.discard(q)

    def publish(self, msg_type: str, data: dict[str, Any]) -> str:
        """Queue one message for every client; a slow client loses its oldest messages."""
        msg = format_sse(msg_type, data)
        for q in list(self._clients):
            if q.full():
                try:
                    q.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            q.put_nowait(msg)
        return msg
