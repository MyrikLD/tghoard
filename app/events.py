import json
from asyncio import Queue
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any


class EventBus:
    """Fan-out of runtime events to SSE subscribers.

    Every subscriber has a bounded queue; when a browser can't keep up the oldest
    events are dropped so workers never block on delivery.
    """

    def __init__(self, maxsize: int = 200) -> None:
        self._subscribers: set[Queue[str]] = set()
        self._maxsize = maxsize

    def publish(self, event: str, data: dict[str, Any]) -> None:
        payload = json.dumps({"event": event, **data}, default=str, ensure_ascii=False)
        for queue in self._subscribers:
            if queue.full():
                queue.get_nowait()
            queue.put_nowait(payload)

    @asynccontextmanager
    async def subscribe(self) -> AsyncIterator[Queue[str]]:
        queue: Queue[str] = Queue(maxsize=self._maxsize)
        self._subscribers.add(queue)
        try:
            yield queue
        finally:
            self._subscribers.discard(queue)


bus = EventBus()
