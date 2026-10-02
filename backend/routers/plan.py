"""Response plan and review endpoints."""

from datetime import datetime

from fastapi import APIRouter, HTTPException, Request

from rate_limit import limiter
from services.store import run_store
from state import PlanItemStatus, ReviewRequest

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
@limiter.limit("10/minute")
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

    if not pending and not rejected:
        run.plan.is_final = True
        run.plan.approved_at = datetime.utcnow()

    run_store.update_run(run)
    return run.plan.model_dump()
