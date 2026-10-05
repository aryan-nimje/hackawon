"""Endpoints the Simulation app pushes to. Intentionally NOT rate-limited
(/sim/world is called ~3x per second)."""

from __future__ import annotations

import math
from typing import Any, Dict, List, Literal, Optional, Union

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from services.bus import bus

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
]


class SimEvent(BaseModel):
    kind: SimEventKind
    text: str = Field(max_length=1000)
    at: Union[int, float, str]
    disruption_id: Optional[str] = Field(default=None, max_length=200)
    incident_id: Optional[str] = Field(default=None, max_length=200)
    urgency: Optional[str] = Field(default=None, max_length=20)


@router.post("/world")
async def post_world(world: WorldState):
    data = world.model_dump(mode="json")
    bus.publish("world", data)  # also stored as the latest world for replay
    return {"ok": True}


@router.post("/event")
async def post_event(event: SimEvent):
    # Broadcast only; never retained.
    bus.publish("sim_event", event.model_dump(mode="json", exclude_none=True))
    return {"ok": True}
