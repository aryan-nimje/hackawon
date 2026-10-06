"""GET /bus/stream — the single SSE feed the three frontends listen to."""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

from services.bus import bus
from services.city import effective_city_slug
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
            if run_store.active_run_id:
                active = run_store.get_active_run()
                yield bus.frame("run.active", {"run_id": run_store.active_run_id, "city": effective_city_slug(active.city) if active else None})
            # The last world snapshot is only replayed while it belongs to the run clients follow. One tagged with a
            # run that is gone (reset, deleted from the database) or that is not the active one is stale.
            world = bus.latest_world
            if world is not None and (world.get("run_id") is None or world.get("run_id") == run_store.active_run_id):
                yield bus.frame("world", world)
            run = run_store.get_active_run()  # no active run: nothing to replay (the newest old run is not "current")
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
