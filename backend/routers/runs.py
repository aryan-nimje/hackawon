"""Run state and SSE stream endpoints."""

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse

from services.events import event_bus
from services.store import run_store

router = APIRouter(prefix="/runs", tags=["runs"])


@router.get("/active")
async def get_active_run():
    """The run every client should follow (set when a scenario starts)."""
    run = run_store.get_active_run()
    return {"run_id": run.id if run else None, "status": run.status.value if run else None, "city": run.city if run else None}


@router.get("/{run_id}")
async def get_run(run_id: str):
    run = run_store.get_run(run_id)
    if not run:
        raise HTTPException(404, "Run not found")
    return run.model_dump()


@router.get("/{run_id}/stream")
async def stream_run(run_id: str):
    run = run_store.get_run(run_id)
    if not run:
        raise HTTPException(404, "Run not found")

    async def event_generator():
        async for payload in event_bus.subscribe(run_id):
            yield f"data: {payload}\n\n"

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
