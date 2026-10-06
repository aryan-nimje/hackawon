"""Endpoints the Simulation app pushes to. Intentionally NOT rate-limited
(/sim/world is called ~3x per second)."""

from __future__ import annotations

import math
from typing import Any, Dict, List, Literal, Optional, Union

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from rate_limit import limiter
from services import sim_evidence, sim_reset
from services.bus import bus
from services.hospital_state import hospital_store
from services.signals.store import signal_store
from services.store import run_store
from state import IncidentSource

router = APIRouter(prefix="/sim", tags=["sim"])

# Keys that carry coordinates anywhere inside the world payload.
_LAT_KEYS = {"lat", "latitude"}
_LNG_KEYS = {"lng", "lon", "long", "longitude"}


def _is_number(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def _check_coords(node: Any, path: str, errors: List[str]) -> None:
    """Recursively validate every lat/lng found; a bad one would crash every open map."""
    if isinstance(node, dict):
        for key, val in node.items():
            k = key.lower() if isinstance(key, str) else key
            here = f"{path}.{key}"
            if k in _LAT_KEYS:
                if not _is_number(val) or not -90 <= val <= 90:
                    errors.append(f"{here}: invalid latitude {val!r}")
            elif k in _LNG_KEYS:
                if not _is_number(val) or not -180 <= val <= 180:
                    errors.append(f"{here}: invalid longitude {val!r}")
            else:
                _check_coords(val, here, errors)
    elif isinstance(node, list):
        for i, item in enumerate(node):
            _check_coords(item, f"{path}[{i}]", errors)


class WorldState(BaseModel):
    """Full world snapshot. Extra keys are kept and forwarded untouched; the
    per-item shape is validated only for the required top-level keys and coordinates."""

    model_config = ConfigDict(extra="allow")

    vehicles: List[Dict[str, Any]]
    disruptions: List[Dict[str, Any]]
    affected_regions: List[Dict[str, Any]]
    sim_incidents: List[Dict[str, Any]]
    execution: List[Dict[str, Any]]
    events: List[Dict[str, Any]]
    beds: Dict[str, Any]
    urgency_override: Dict[str, Any]
    sim_time_s: float
    ts: Union[int, float, str]
    run_id: Optional[str] = None  # tagged with the active run when the sender omits it
    # Hospital census: per hospital occupancy, patients' stays, overflow. Optional so older senders still work.
    hospital_load: Optional[Dict[str, Any]] = None
    admitted_incident_ids: Optional[List[str]] = None

    @field_validator("sim_time_s")
    @classmethod
    def _finite_time(cls, v: float) -> float:
        if not math.isfinite(v):
            raise ValueError("sim_time_s must be finite")
        return v

    @model_validator(mode="after")
    def _validate_coordinates(self) -> "WorldState":
        errors: List[str] = []
        _check_coords(self.model_dump(), "world", errors)
        if errors:
            shown = "; ".join(errors[:5])
            more = f" (+{len(errors) - 5} more)" if len(errors) > 5 else ""
            raise ValueError(f"bad coordinates: {shown}{more}")
        return self


SimEventKind = Literal[
    "vehicle_failed",
    "route_blocked",
    "bridge_collapsed",
    "site_inaccessible",
    "incident_added",
    "incident_escalated",
    "region_added",
    "region_expanded",
    "fault_cleared",
    "hospital_full",
    "patient_diverted",
]


class SimEvent(BaseModel):
    kind: SimEventKind
    text: str = Field(max_length=1000)
    at: Union[int, float, str]
    disruption_id: Optional[str] = Field(default=None, max_length=200)
    incident_id: Optional[str] = Field(default=None, max_length=200)
    run_id: Optional[str] = Field(default=None, max_length=200)
    urgency: Optional[str] = Field(default=None, max_length=20)


@router.post("/world")
async def post_world(world: WorldState):
    if world.run_id and run_store.is_removed(world.run_id):
        return {"ok": True, "ignored": True}  # a snapshot from a run that "Reset simulation" already removed
    data = world.model_dump(mode="json")
    if "run_id" not in world.model_fields_set:
        # Sender omitted the tag: attach the active run. An explicit null (idle sim, no run yet) stays null.
        data["run_id"] = run_store.active_run_id
    for key in ("hospital_load", "admitted_incident_ids"):
        if data.get(key) is None:
            data.pop(key, None)  # absent, not null, when the sender has no census
    hospital_store.update_from_world(data)  # the Medical Agent plans against this occupancy
    bus.publish("world", data)  # also stored as the latest world for replay
    return {"ok": True}


@router.post("/event")
async def post_event(event: SimEvent):
    # Broadcast only; never retained.
    bus.publish("sim_event", event.model_dump(mode="json", exclude_none=True))
    return {"ok": True}


# ───────────────────────── simulated evidence + reset ─────────────────────────
class EvidenceRequest(BaseModel):
    """One evidence action for ONE incident. No form: the evidence is built from the incident itself."""

    run_id: str = Field(min_length=1, max_length=200)
    incident_id: str = Field(min_length=1, max_length=200)
    action: Literal["news", "official_alert", "weather", "all_clear", "normal_conditions", "random"]


@router.post("/evidence")
@limiter.limit("120/minute")
async def post_evidence(body: EvidenceRequest, request: Request):
    """Add simulated evidence about one incident, then score the incident again.

    The evidence is an ordinary signal in the common signal store (tagged `metadata.simulated`), so it is scored by the
    normal Verification Agent: backing evidence through the supporting path, contradicting evidence through the
    contradiction path. `random` picks supporting or contradicting evidence for THIS incident only (a fresh draw per call).
    """
    from supervisor import rescore_incidents  # late import: supervisor imports a lot

    run = run_store.get_run(body.run_id)
    if run is None:
        raise HTTPException(404, "Run not found")
    incident = next((i for i in run.incidents if i.id == body.incident_id), None)
    if incident is None:
        raise HTTPException(404, "Incident not found in this run")
    if incident.source in (IncidentSource.WEATHER, IncidentSource.NEWS):
        raise HTTPException(422, "Weather and news incidents are evidence themselves; evidence applies to reports.")

    kind = sim_evidence.resolve_action(body.action)
    try:
        sig = sim_evidence.build_signal(incident, kind, context=run.incidents)
    except sim_evidence.NoHazard as exc:
        raise HTTPException(422, str(exc))
    signal_store.upsert_many([sig])  # the same store the real sources write to

    results = await rescore_incidents(run.id, {incident.id})
    ver = next((v for v in results if v.incident_id == incident.id), None)
    return {
        "ok": True,
        "incident_id": incident.id,
        "action": body.action,
        "evidence": kind,
        "stance": sim_evidence.stance_of(kind),
        "signal_id": sig.id,
        "credibility": ver.credibility if ver else None,
        "verification": ver.model_dump(mode="json") if ver else None,
    }


@router.post("/reset")
@limiter.limit("20/minute")
async def reset_simulation(request: Request):
    """Remove everything the Simulation app created (runs, incidents, signals, plans, census). Real data is untouched."""
    return await sim_reset.reset_simulation()
