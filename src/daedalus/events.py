"""Event fan-out: one runtime trace stream, many consumers.

`-v` prints it in the terminal; kaminos attaches over SSE; both see the
same events. Publishing never blocks the agent: slow consumers drop.
"""

from __future__ import annotations

import asyncio
from collections import deque

_REPLAY = 100
_QUEUE_CAP = 500


class EventBus:
    def __init__(self, replay: int = _REPLAY):
        self._subscribers: set[asyncio.Queue] = set()
        self._replay: deque[dict] = deque(maxlen=replay)

    def publish(self, event: dict) -> None:
        self._replay.append(event)
        for queue in list(self._subscribers):
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                pass  # a stalled dashboard must never stall the agent

    def subscribe(self) -> tuple[list[dict], asyncio.Queue]:
        """Returns (replay of recent events, live queue). Late attachers see
        how the task got here — the kaminos trace pane opens mid-story."""
        queue: asyncio.Queue = asyncio.Queue(maxsize=_QUEUE_CAP)
        self._subscribers.add(queue)
        return list(self._replay), queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        self._subscribers.discard(queue)
