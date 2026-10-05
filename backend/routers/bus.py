"""GET /bus/stream — the single SSE feed the three frontends listen to."""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

from services.bus import bus
from services.plan_payload import build_plan_payload
from services.store import run_store

router = APIRouter(prefix="/bus", tags=["bus"])

KEEPALIVE_SECONDS = 15.0


@router.get("/stream")
async def bus_stream(request: Request):
    async def event_generator():
        # Register first so nothing published during the replay is missed.
        queue = bus.register()
        try:
            yield "retry: 3000\n\n"
            yield ": connected\n\n"

            # Replay current state so a dashboard opened mid-run isn't empty.
            if bus.latest_world is not None:
                yield bus.frame("world", bus.latest_world)
            run = run_store.get_latest_run()
            if run is not None:
                yield bus.frame("plan.updated", build_plan_payload(run))

            while True:
                try:
                    msg = await asyncio.wait_for(queue.get(), timeout=KEEPALIVE_SECONDS)
                except asyncio.TimeoutError:
                    if await request.is_disconnected():
                        break
                    yield ": keepalive\n\n"
                    continue
                yield msg
        finally:
            bus.unregister(queue)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
