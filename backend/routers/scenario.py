"""Scenario control endpoints."""

import asyncio
import uuid

from fastapi import APIRouter, Request

from config import get_settings
from rate_limit import limiter
from services.store import run_store
from state import RunStatus, ScenarioStartRequest
from supervisor import start_scenario_replay

router = APIRouter(prefix="/scenario", tags=["scenario"])

# Track pending run ids started via API
_pending_runs: dict[str, str] = {}


@router.post("/start")
@limiter.limit("5/minute")
async def start_scenario(body: ScenarioStartRequest, request: Request):
    settings = get_settings()
    run_token = str(uuid.uuid4())
    _pending_runs[run_token] = "starting"

    async def _run() -> None:
        run = await start_scenario_replay(body.replay_speed, body.simulate_failures)
        _pending_runs[run_token] = run.id

    task = asyncio.create_task(_run())

    # Wait briefly for run to be created
    for _ in range(20):
        await asyncio.sleep(0.05)
        latest = run_store.get_latest_run()
        if latest and latest.status in (RunStatus.RUNNING, RunStatus.COMPLETED):
            return {
                "message": "Scenario started",
                "run_id": latest.id,
                "mock_mode": settings.effective_mock_mode,
            }

    return {
        "message": "Scenario starting",
        "run_id": run_store.get_latest_run().id if run_store.get_latest_run() else run_token,
        "mock_mode": settings.effective_mock_mode,
    }
