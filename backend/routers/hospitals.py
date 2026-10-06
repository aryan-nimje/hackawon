"""GET /hospitals/capacity — what the Medical Agent sees: live occupancy when the simulator is running."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter

from services.city import city_context
from services.data_loader import load_hospitals
from services.hospital_state import hospital_store
from services.store import run_store

router = APIRouter(prefix="/hospitals", tags=["hospitals"])


@router.get("/capacity")
async def hospital_capacity(run_id: Optional[str] = None) -> Dict[str, Any]:
    """Per hospital: capacity, occupied, free and status. `live` is false when only static layer data exists
    (no simulator census for this run), in which case occupied is 0 and free is the layer's bed count."""
    rid = run_id or run_store.active_run_id
    live = hospital_store.is_live(rid)
    hospitals: List[Dict[str, Any]] = []
    run = run_store.get_run(rid) if rid else None
    with city_context(run.city if run else None):
        base = load_hospitals()
    for h in hospital_store.overlay(base, rid):
        census = hospital_store.get(h["id"]) if live else None
        cap = int(h.get("capacity", h.get("beds", 0)))
        free = int(h.get("beds_available", cap))
        hospitals.append({
            "id": h["id"],
            "name": h["name"],
            "capacity": cap,
            "occupied": cap - free,
            "free": free,
            "occupancy": round((cap - free) / cap, 3) if cap else 0.0,
            "status": h.get("status", "operational"),
            **({k: census[k] for k in (
                "demand_multiplier", "walk_in_patients", "incident_patients", "walk_ins_total", "discharged_total",
                "diverted_in", "diverted_out", "overflow", "next_discharge_s", "patients",
            ) if k in census} if census else {}),
        })
    return {"live": live, "run_id": rid, "sim_time_s": hospital_store.sim_time_s if live else None, "hospitals": hospitals}
