"""Alert draft endpoints."""

from fastapi import APIRouter, HTTPException

from services.store import run_store

router = APIRouter(prefix="/alerts", tags=["alerts"])


@router.get("/{run_id}")
async def get_alerts(run_id: str):
    run = run_store.get_run(run_id)
    if not run:
        raise HTTPException(404, "Run not found")
    return [a.model_dump() for a in run.alerts]
