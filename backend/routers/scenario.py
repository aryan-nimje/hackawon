"""Scenario control endpoints."""

import asyncio
import uuid

from fastapi import APIRouter, Request

from fastapi import HTTPException

from config import get_settings
from rate_limit import limiter
from services import citizen_db
from services.resync import resync_from_db
from services.city import LayersUnavailable, city_for_point, get_layers
from services.osm import slugify
from services.store import run_store
from state import RunStatus, ScenarioStartRequest
from supervisor import start_scenario_replay

router = APIRouter(prefix="/scenario", tags=["scenario"])

# Track pending run ids started via API
_pending_runs: dict[str, str] = {}
_background_tasks: set = set()  # keep references so running scenarios are not garbage-collected


@router.post("/start")
@limiter.limit("30/minute")
async def start_scenario(body: ScenarioStartRequest, request: Request):
    settings = get_settings()
    resync_from_db()  # memory is only a cache of the database: drop what was deleted there before a new run starts
    city_slug = None
    if body.city and body.city.strip():
        # Make sure the city's layers exist (cached file, or fetched from OSM now) before a run is built on them.
        try:
            layers = await get_layers(body.city)
        except LayersUnavailable as exc:
            raise HTTPException(exc.status_code, exc.message)
        city_slug = layers["city"].get("slug") or slugify(body.city)
    elif citizen_db.configured():
        # No city named (live mode): plan for the city of the newest real citizen report, if it is not the default one.
        try:
            recent = await asyncio.to_thread(citizen_db.fetch_recent)
            if recent:
                city_slug = await city_for_point(recent[0]["lat"], recent[0]["lng"])
        except Exception:  # city detection must never stop a run from starting
            city_slug = None
    run_token = str(uuid.uuid4())
    _pending_runs[run_token] = "starting"

    async def _run() -> None:
        run = await start_scenario_replay(body.replay_speed, body.simulate_failures, city_slug, body.simulation)
        _pending_runs[run_token] = run.id

    previous = run_store.active_run_id
    task = asyncio.create_task(_run())
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)

    # Wait briefly for the new run to become the active one
    for _ in range(40):
        await asyncio.sleep(0.05)
        active = run_store.get_active_run()
        if active and active.id != previous:
            return {
                "message": "Scenario started",
                "run_id": active.id,
                "active_run_id": active.id,
                "mock_mode": settings.effective_mock_mode,
            }

    return {
        "message": "Scenario starting",
        "run_id": run_token,
        "active_run_id": run_store.active_run_id,
        "mock_mode": settings.effective_mock_mode,
    }
