"""Incident endpoints: listing, and creating a manual incident."""

import uuid
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, HTTPException, Request

from rate_limit import limiter
from services.city import LayersUnavailable, city_for_point, get_layers
from services.osm import slugify
from services.store import run_store
from state import Incident, IncidentCreate, IncidentSource
from supervisor import register_incident, schedule_incident_planning, start_run_for_incident

router = APIRouter(tags=["incidents"])


@router.get("/incidents")
async def list_incidents(run_id: Optional[str] = None, all: bool = False):
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

    # Default view: the active run only (older runs are hidden, not deleted). `?all=true` for everything.
    active = run_store.get_active_run()
    if not all and active:
        return await list_incidents(run_id=active.id)
    ver_map = {v.incident_id: v for v in active.verifications} if active else {}
    return [
        {
            **inc.model_dump(),
            "verification": ver_map.get(inc.id).model_dump() if inc.id in ver_map else None,
        }
        for inc in run_store.list_incidents()
    ]


@router.post("/incidents", status_code=201)
@limiter.limit("30/minute")
async def create_incident(body: IncidentCreate, request: Request):
    """Create a real incident and send it through the agent planning workflow.

    It is added to the given run (default: the active run; a new run is started if there is none).
    The incident is visible to every client immediately; its plan items arrive over the live stream
    (`plan.updated`) as the agents finish, then wait for Authority approval like any other item.
    """
    meta = {"manual": True, "sim_added": True}
    if body.people is not None:
        meta["people"] = body.people
    if body.vulnerable:
        meta["vulnerable"] = body.vulnerable
    incident = Incident(
        id=f"manual-{uuid.uuid4().hex[:8]}",
        text=body.text,
        location=(body.location or "").strip() or f"{body.lat:.4f}, {body.lng:.4f}",
        lat=body.lat,
        lng=body.lng,
        need_type=body.need_type,
        urgency=body.urgency,
        source=IncidentSource.SIM,
        timestamp=datetime.utcnow(),
        raw_metadata=meta,
    )

    if body.run_id:
        run = run_store.get_run(body.run_id)
        if not run:
            raise HTTPException(404, "Run not found")
    else:
        run = run_store.get_active_run()

    if run is None:
        city_slug = None
        if body.city and body.city.strip():
            try:
                city_slug = (await get_layers(body.city))["city"].get("slug") or slugify(body.city)
            except LayersUnavailable as exc:
                raise HTTPException(exc.status_code, exc.message)
        else:
            # No city named (a real incident, not the simulator): plan for the city the incident is in.
            city_slug = await city_for_point(body.lat, body.lng)
        run = await start_run_for_incident(incident, city_slug)  # the pipeline plans it as part of the new run
    else:
        await register_incident(run, incident)
        schedule_incident_planning(run.id, incident.id)

    return {**incident.model_dump(mode="json"), "verification": None, "run_id": run.id}
