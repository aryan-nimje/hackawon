"""Route Agent — compute routes with blocked zone detection."""

from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional, Sequence

from pydantic import BaseModel

from services.city import get_city
from services.data_loader import load_blocked_zones, load_hospitals, load_inventory
from services.routing import (
    Hazard,
    find_clean_route,
    get_route,
    hazards_from_world,
    polygon_hazard,
)
from state import Incident, RouteInfo, RouteOutput, RunState

NO_CLEAN_DETOUR = "no clean detour found"


class RerouteOutput(BaseModel):
    """Result of a Route Agent re-route request (POST /plan/{run_id}/replan)."""

    status: Literal["rerouted", "no_clean_detour", "no_vehicle", "no_route", "unsupported"]
    route: Optional[RouteInfo] = None
    message: str = ""
    reason: str = ""


async def _leg(from_lat: float, from_lng: float, to_lat: float, to_lng: float, blocked):
    """(dist_km, dur_min, geometry, blocked_warning, rerouted). A blocked fastest route is replaced
    by the fastest clean OSRM alternative when one exists (distance/time are OSRM's)."""
    dist, dur, geom, warn = await get_route(from_lat, from_lng, to_lat, to_lng, blocked)
    if not warn:
        return dist, dur, geom, False, False
    hazards = [polygon_hazard(f"zone-{i}", poly) for i, poly in enumerate(blocked)]
    found = await find_clean_route((from_lat, from_lng), (to_lat, to_lng), hazards)
    if found.route:
        c = found.route
        return c.distance_km, c.duration_min, c.geometry, False, True
    return dist, dur, geom, True, False


async def run_route(state: RunState) -> RouteOutput:
    """Compute routes for rescue, medical, and logistics assignments."""
    blocked = load_blocked_zones()
    incident_map = {i.id: i for i in state.incidents}
    hospitals = {h["id"]: h for h in load_hospitals()}
    warehouses = {w["id"]: w for w in load_inventory()}

    # Depot: the relief depot nearest the city centre acts as the staging area
    c_lat, c_lng = get_city()["center"]
    staging = min(warehouses.values(), key=lambda w: (w["lat"] - c_lat) ** 2 + (w["lng"] - c_lng) ** 2) if warehouses else None
    depot_lat, depot_lng = (staging["lat"], staging["lng"]) if staging else (c_lat, c_lng)
    routes: List[RouteInfo] = []
    needs_revision = False
    revision_reason = ""

    for assignment in state.rescue_queue:
        inc = incident_map.get(assignment.incident_id)
        if not inc:
            continue
        dist, dur, geom, blocked_warn, rerouted = await _leg(
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
                    + (" Re-routed around a flooded zone (fastest clean OSRM route)." if rerouted else "")
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
        dist, dur, geom, blocked_warn, rerouted = await _leg(
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
                explanation=f"Patient transport to {hosp['name']}." + (" Re-routed around a flooded zone." if rerouted else ""),
            )
        )

    for alloc in state.supply_allocations:
        wh = warehouses.get(alloc.warehouse_id)
        zone = next((z for z in state.zones if z.id == alloc.zone_id), None)
        if not wh or not zone:
            continue
        dist, dur, geom, blocked_warn, rerouted = await _leg(
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
                explanation=f"Supply convoy from {wh['name']} to {zone.name}." + (" Re-routed around a flooded zone." if rerouted else ""),
            )
        )

    return RouteOutput(routes=routes, needs_revision=needs_revision, revision_reason=revision_reason)


async def run_reroute(
    assignment_id: str,
    assignment_type: str,
    origin: tuple,
    dest: tuple,
    hazards: Sequence[Hazard],
    heading: Optional[float] = None,
) -> RerouteOutput:
    """New route for a blocked vehicle: from its CURRENT position to the original destination.

    Finds the fastest clean OSRM route (alternatives, then via-points) and re-checks it against
    the hazards with the blocking rule. No clean route -> status "no_clean_detour".

    A moving vehicle cannot U-turn on the spot: when its heading is known the first search only accepts routes that
    leave the vehicle's position in its direction of travel (it turns off at the next junction). Only when no such
    clean route exists is a route that turns the vehicle round accepted, and the explanation says so."""
    turned_round = False
    found = await find_clean_route(origin, dest, hazards, heading) if heading is not None else await find_clean_route(origin, dest, hazards)
    if not found.route and heading is not None:
        found = await find_clean_route(origin, dest, hazards)
        turned_round = bool(found.route)
    if not found.route:
        return RerouteOutput(status="no_clean_detour", message=NO_CLEAN_DETOUR, reason=found.reason)
    c = found.route
    avoided = ", ".join(found.avoided)
    return RerouteOutput(
        status="rerouted",
        reason="ok",
        route=RouteInfo(
            assignment_id=assignment_id,
            assignment_type=assignment_type,  # type: ignore[arg-type]
            from_lat=origin[0],
            from_lng=origin[1],
            to_lat=dest[0],
            to_lng=dest[1],
            distance_km=round(c.distance_km, 2),
            duration_min=round(c.duration_min, 1),
            geometry=c.geometry,
            blocked_warning=False,
            explanation=(
                "Re-routed from the vehicle's current position via the fastest clean OSRM route"
                + (" (no forward detour exists, so the vehicle has to turn round)" if turned_round else " without turning round" if heading is not None else "")
                + (f" (avoids: {avoided})" if avoided else "")
                + (f", {len(c.vias)} via-point(s)" if c.vias else "")
                + "."
            ),
        ),
    )


async def reroute_plan_item(run: RunState, item_id: str, world: Optional[Dict[str, Any]]) -> RerouteOutput:
    """Resolve a plan item ("rescue-<id>", "medical-<id>", "logistics-<zone>") to its vehicle and
    destination, gather the live hazards, and ask the Route Agent for a new route."""
    kind, _, target = item_id.partition("-")
    if kind not in ("rescue", "medical", "logistics") or not target:
        return RerouteOutput(status="unsupported", message="Only rescue, medical and logistics items can be re-routed.")

    current = next((r for r in run.routes if r.assignment_type == kind and r.assignment_id == target), None)
    if current is None:
        return RerouteOutput(status="no_route", message="No planned route for this item.")

    live = world if world and world.get("run_id") in (None, run.id) else None
    vehicle = None
    if live:
        vehicle = next(
            (v for v in live.get("vehicles") or [] if v.get("kind") == kind and v.get("target_id") == target),
            None,
        )
    if vehicle is None:
        return RerouteOutput(status="no_vehicle", message="No vehicle position reported for this item.")

    # Hazards as the simulator sees them right now; fall back to the static flood zones.
    hazards: List[Hazard] = hazards_from_world(live)
    if not (live.get("affected_regions") if live else None):
        hazards += [polygon_hazard(f"zone-{i}", poly) for i, poly in enumerate(load_blocked_zones())]

    return await run_reroute(
        target,
        kind,
        (float(vehicle["lat"]), float(vehicle["lng"])),
        (current.to_lat, current.to_lng),
        hazards,
        heading=float(vehicle["heading"]) if isinstance(vehicle.get("heading"), (int, float)) else None,
    )
