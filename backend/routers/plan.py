"""Response plan and review endpoints."""

from datetime import datetime

from fastapi import APIRouter, HTTPException, Request

from rate_limit import limiter
from services.bus import bus
from services.plan_payload import broadcast_plan
from services.store import run_store
from state import PlanItemStatus, ReplanRequest, ReviewRequest

router = APIRouter(prefix="/plan", tags=["plan"])


@router.get("/{run_id}")
async def get_plan(run_id: str):
    run = run_store.get_run(run_id)
    if not run:
        raise HTTPException(404, "Run not found")
    if not run.plan:
        raise HTTPException(404, "Plan not yet generated")
    return run.plan.model_dump()


@router.post("/{run_id}/review")
@limiter.limit("60/minute")  # UI sends one request per Approve click
async def review_plan(run_id: str, body: ReviewRequest, request: Request):
    run = run_store.get_run(run_id)
    if not run or not run.plan:
        raise HTTPException(404, "Plan not found")

    item_map = {item.id: item for item in run.plan.items}
    for action in body.actions:
        item = item_map.get(action.item_id)
        if not item:
            continue
        if action.action == "approve":
            item.status = PlanItemStatus.APPROVED
        elif action.action == "reject":
            item.status = PlanItemStatus.REJECTED
        elif action.action == "edit":
            item.status = PlanItemStatus.EDITED
            item.edited_content = action.edited_content

    pending = [i for i in run.plan.items if i.status == PlanItemStatus.PENDING]
    rejected = [i for i in run.plan.items if i.status == PlanItemStatus.REJECTED]

    newly_final = False
    if not pending and not rejected:
        if not run.plan.is_final:
            newly_final = True
        run.plan.is_final = True
        if run.plan.approved_at is None:
            run.plan.approved_at = datetime.utcnow()

    run_store.update_run(run)
    broadcast_plan(run, "plan.updated")
    if newly_final:  # exactly once: the sim dispatches vehicles on this
        broadcast_plan(run, "plan.approved")
    return run.plan.model_dump()


@router.post("/{run_id}/replan")
async def replan(run_id: str, body: ReplanRequest):
    """Authority -> sim: a vehicle is blocked; reroute or hold the plan item."""
    run = run_store.get_run(run_id)
    if not run or not run.plan:
        raise HTTPException(404, "Run or plan not found")
    if not any(i.id == body.item_id for i in run.plan.items):
        raise HTTPException(404, "Plan item not found")
    bus.publish("plan.replan", {"item_id": body.item_id, "action": body.action})
    return {"ok": True, "item_id": body.item_id, "action": body.action}
