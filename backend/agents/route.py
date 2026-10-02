"""Route Agent — compute routes with blocked zone detection."""

from __future__ import annotations

from typing import List

from services.data_loader import load_blocked_zones, load_hospitals, load_inventory
from services.routing import get_route
from state import Incident, RouteInfo, RouteOutput, RunState


async def run_route(state: RunState) -> RouteOutput:
    """Compute routes for rescue, medical, and logistics assignments."""
    blocked = load_blocked_zones()
    incident_map = {i.id: i for i in state.incidents}
    hospitals = {h["id"]: h for h in load_hospitals()}
    warehouses = {w["id"]: w for w in load_inventory()}

    # Depot: downtown staging area
    depot_lat, depot_lng = 29.7523, -95.3589
    routes: List[RouteInfo] = []
    needs_revision = False
    revision_reason = ""

    for assignment in state.rescue_queue[:10]:
        inc = incident_map.get(assignment.incident_id)
        if not inc:
            continue
        dist, dur, geom, blocked_warn = await get_route(
            depot_lat, depot_lng, inc.lat, inc.lng, blocked
        )
        routes.append(
            RouteInfo(
                assignment_id=assignment.incident_id,
                assignment_type="rescue",
                from_lat=depot_lat,
                from_lng=depot_lng,
                to_lat=inc.lat,
                to_lng=inc.lng,
                distance_km=round(dist, 2),
                duration_min=round(dur, 1),
                geometry=geom,
                blocked_warning=blocked_warn,
                explanation=(
                    "Route via staging depot to rescue site."
                    + (" WARNING: intersects flooded zone — alternate route required." if blocked_warn else "")
                ),
            )
        )
        if blocked_warn and state.revision_count == 0:
            needs_revision = True
            revision_reason = f"Rescue route to {inc.id} crosses blocked flood zone"

    for assignment in state.hospital_assignments:
        inc = incident_map.get(assignment.incident_id)
        hosp = hospitals.get(assignment.hospital_id)
        if not inc or not hosp:
            continue
        dist, dur, geom, blocked_warn = await get_route(
            inc.lat, inc.lng, hosp["lat"], hosp["lng"], blocked
        )
        routes.append(
            RouteInfo(
                assignment_id=assignment.incident_id,
                assignment_type="medical",
                from_lat=inc.lat,
                from_lng=inc.lng,
                to_lat=hosp["lat"],
                to_lng=hosp["lng"],
                distance_km=round(dist, 2),
                duration_min=round(dur, 1),
                geometry=geom,
                blocked_warning=blocked_warn,
                explanation=f"Patient transport to {hosp['name']}.",
            )
        )

    for alloc in state.supply_allocations:
        wh = warehouses.get(alloc.warehouse_id)
        zone = next((z for z in state.zones if z.id == alloc.zone_id), None)
        if not wh or not zone:
            continue
        dist, dur, geom, blocked_warn = await get_route(
            wh["lat"], wh["lng"], zone.center_lat, zone.center_lng, blocked
        )
        routes.append(
            RouteInfo(
                assignment_id=alloc.zone_id,
                assignment_type="logistics",
                from_lat=wh["lat"],
                from_lng=wh["lng"],
                to_lat=zone.center_lat,
                to_lng=zone.center_lng,
                distance_km=round(dist, 2),
                duration_min=round(dur, 1),
                geometry=geom,
                blocked_warning=blocked_warn,
                explanation=f"Supply convoy from {wh['name']} to {zone.name}.",
            )
        )

    return RouteOutput(routes=routes, needs_revision=needs_revision, revision_reason=revision_reason)
