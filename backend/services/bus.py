"""Global named-event bus feeding GET /bus/stream (SSE).

Deliberately dependency-free (stdlib only). Everything runs on the single uvicorn
event loop, so no locking is needed. State lives in this process: run uvicorn with
ONE worker or subscribers on other workers will never see published events.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Optional, Set

logger = logging.getLogger(__name__)

QUEUE_SIZE = 1000


class BusHub:
    def __init__(self, queue_size: int = QUEUE_SIZE) -> None:
        self._queue_size = queue_size
        self._subs: Set[asyncio.Queue[str]] = set()
        # Latest world snapshot, replayed to every new subscriber.
        self.latest_world: Optional[dict] = None

    # -- framing -----------------------------------------------------------
    @staticmethod
    def frame(event: str, data: Any) -> str:
        """One SSE message with a *named* event: `event: x\\ndata: {...}\\n\\n`."""
        body = json.dumps(data, separators=(",", ":"), default=str, allow_nan=False)
        return f"event: {event}\ndata: {body}\n\n"

    # -- subscribers -------------------------------------------------------
    @property
    def has_subscribers(self) -> bool:
        return bool(self._subs)

    @property
    def subscriber_count(self) -> int:
        return len(self._subs)

    def register(self) -> "asyncio.Queue[str]":
        q: asyncio.Queue[str] = asyncio.Queue(maxsize=self._queue_size)
        self._subs.add(q)
        return q

    def unregister(self, q: "asyncio.Queue[str]") -> None:
        self._subs.discard(q)

    # -- publishing --------------------------------------------------------
    def publish(self, event: str, data: Any) -> None:
        """Broadcast to all subscribers. Never raises into the caller."""
        try:
            msg = self.frame(event, data)
        except (TypeError, ValueError):
            logger.exception("Could not serialise bus event %r", event)
            return
        # Only keep a snapshot that serialised, so replay-on-connect can never fail.
        if event == "world" and isinstance(data, dict):
            self.latest_world = data
        for q in list(self._subs):
            if q.full():
                # A slow client: drop its oldest message rather than the client.
                # Safe because world/plan payloads are full snapshots.
                try:
                    q.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            try:
                q.put_nowait(msg)
            except asyncio.QueueFull:  # pragma: no cover - raced, ignore
                pass


bus = BusHub()
