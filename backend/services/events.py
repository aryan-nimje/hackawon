"""SSE event broadcasting for agent activity."""

from __future__ import annotations

import asyncio
import json
from collections import defaultdict
from typing import AsyncGenerator, Dict, List

from state import ActivityEvent


class EventBus:
    """In-memory pub/sub for run activity events."""

    def __init__(self) -> None:
        self._subscribers: Dict[str, List[asyncio.Queue[str]]] = defaultdict(list)
        self._history: Dict[str, List[ActivityEvent]] = defaultdict(list)

    def publish(self, event: ActivityEvent) -> None:
        self._history[event.run_id].append(event)
        payload = event.model_dump_json()
        dead: List[asyncio.Queue[str]] = []
        for queue in self._subscribers[event.run_id]:
            try:
                queue.put_nowait(payload)
            except asyncio.QueueFull:
                dead.append(queue)
        for q in dead:
            if q in self._subscribers[event.run_id]:
                self._subscribers[event.run_id].remove(q)

    async def subscribe(self, run_id: str) -> AsyncGenerator[str, None]:
        queue: asyncio.Queue[str] = asyncio.Queue(maxsize=100)
        self._subscribers[run_id].append(queue)
        try:
            for past in self._history.get(run_id, []):
                yield past.model_dump_json()
            while True:
                msg = await queue.get()
                yield msg
        finally:
            if queue in self._subscribers[run_id]:
                self._subscribers[run_id].remove(queue)

    def forget(self, run_id: str) -> None:
        """Drop a run's activity history (used when a simulation run is removed)."""
        self._history.pop(run_id, None)
        self._subscribers.pop(run_id, None)

    def get_history(self, run_id: str) -> List[ActivityEvent]:
        return list(self._history.get(run_id, []))


event_bus = EventBus()
