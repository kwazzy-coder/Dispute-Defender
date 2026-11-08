"""Tiny in-process pub/sub that fans server events out to every open dashboard (Server-Sent Events)."""

import asyncio
import json
from datetime import datetime, timezone


class EventBus:
    def __init__(self) -> None:
        self._subscribers: set[asyncio.Queue] = set()

    def subscribe(self) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=200)
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        self._subscribers.discard(queue)

    def publish(self, kind: str, **payload) -> None:
        event = {"kind": kind, "time": datetime.now(timezone.utc).isoformat(), **payload}
        for queue in list(self._subscribers):
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                # A stalled browser tab must not block the agent; drop its oldest event.
                queue.get_nowait()
                queue.put_nowait(event)

    @staticmethod
    def encode(event: dict) -> str:
        return f"data: {json.dumps(event)}\n\n"


bus = EventBus()
