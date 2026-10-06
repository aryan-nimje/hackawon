"""Builds the plan.updated / plan.approved payload and broadcasts it."""

from __future__ import annotations

from typing import Any, Dict

from services.bus import bus
from services.store import run_store
from state import RunState


def build_plan_payload(run: RunState) -> Dict[str, Any]:
    """{run_id, plan|null, routes, hospital_assignments, incidents(+verification),
    zones, alerts, activity_log}. `plan` is null until the supervisor finishes."""
    ver_map = {v.incident_id: v for v in run.verifications}
    incidents = []
    for inc in run.incidents:
        ver = ver_map.get(inc.id)
        incidents.append(
            {
                **inc.model_dump(mode="json"),
                "verification": ver.model_dump(mode="json") if ver else None,
            }
        )
    return {
        "run_id": run.id,
        "plan": run.plan.model_dump(mode="json") if run.plan else None,
        "routes": [r.model_dump(mode="json") for r in run.routes],
        "hospital_assignments": [h.model_dump(mode="json") for h in run.hospital_assignments],
        "incidents": incidents,
        "zones": [z.model_dump(mode="json") for z in run.zones],
        "alerts": [a.model_dump(mode="json") for a in run.alerts],
        "activity_log": [e.model_dump(mode="json") for e in run.activity_log],
    }


def broadcast_plan(run: RunState, event: str = "plan.updated") -> None:
    if not bus.has_subscribers or run_store.is_removed(run.id):
        return
    bus.publish(event, build_plan_payload(run))
