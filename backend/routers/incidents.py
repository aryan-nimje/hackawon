"""Incident listing endpoints."""

from typing import Optional

from fastapi import APIRouter, HTTPException

from services.store import run_store

router = APIRouter(tags=["incidents"])


@router.get("/incidents")
async def list_incidents(run_id: Optional[str] = None):
    if run_id:
        run = run_store.get_run(run_id)
        if not run:
            raise HTTPException(404, "Run not found")
        ver_map = {v.incident_id: v for v in run.verifications}
        return [
            {
                **inc.model_dump(),
                "verification": ver_map.get(inc.id).model_dump() if inc.id in ver_map else None,
            }
            for inc in run.incidents
        ]

    incidents = run_store.list_incidents()
    latest = run_store.get_latest_run()
    ver_map = {}
    if latest:
        ver_map = {v.incident_id: v for v in latest.verifications}

    return [
        {
            **inc.model_dump(),
            "verification": ver_map.get(inc.id).model_dump() if inc.id in ver_map else None,
        }
        for inc in incidents
    ]
