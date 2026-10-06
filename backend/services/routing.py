"""Routing service: OSRM routes, hazard geometry, and clean-detour search.

Blocking rule (single definition, used by the Route Agent and mirrored by the simulator):
a route is blocked by a hazard only when the actual road path crosses that hazard while BOTH
route endpoints are outside it. A rescue going into a hazard (destination inside) or a
patient/vehicle leaving one (origin inside) is never blocked by that hazard.

One exception, for point hazards (a blocked road, a collapsed bridge): a vehicle that stopped at the
blockage is "inside" its small radius, but it may only LEAVE it. A route that drives on towards the
blockage is still blocked, so the vehicle must turn round (U-turn) instead of being sent through it.
"""

from __future__ import annotations

import asyncio
import logging
import math
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import httpx

logger = logging.getLogger(__name__)

OSRM_BASE = "https://router.project-osrm.org/route/v1/driving"

# The simulator blocks a road within 0.16 km of a blocked-road / collapsed-bridge point.
# A small margin here means a route called clean is also clean for the simulator.
POINT_HAZARD_RADIUS_KM = 0.18
# A route that starts inside a point hazard is "leaving" it only if it never gets this much closer to it.
POINT_LEAVE_MARGIN_KM = 0.05
# How far outside a hazard a via-point is placed.
VIA_CLEARANCE_KM = 0.30
# OSRM request budget for one detour search (public OSRM is rate limited).
MAX_VIA_REQUESTS_PER_ROUND = 8
VIA_ROUNDS = 2
OSRM_CONCURRENCY = 4

LatLng = Tuple[float, float]
# (distance_km, duration_min, geometry [[lat, lng], ...])
RouteResult = Tuple[float, float, List[List[float]]]


def haversine_km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def point_in_polygon(lat: float, lng: float, polygon: List[List[float]]) -> bool:
    """Ray casting; polygon coords as [lng, lat]."""
    inside = False
    n = len(polygon)
    j = n - 1
    for i in range(n):
        xi, yi = polygon[i][0], polygon[i][1]
        xj, yj = polygon[j][0], polygon[j][1]
        if ((yi > lat) != (yj > lat)) and (
            lng < (xj - xi) * (lat - yi) / (yj - yi + 1e-12) + xi
        ):
            inside = not inside
        j = i
    return inside


# --------------------------------------------------------------------------- hazards


def _seg_intersect(p1: Sequence[float], p2: Sequence[float], p3: Sequence[float], p4: Sequence[float]) -> bool:
    """Planar segment intersection (points as (x, y))."""

    def orient(a: Sequence[float], b: Sequence[float], c: Sequence[float]) -> float:
        return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])

    d1, d2 = orient(p3, p4, p1), orient(p3, p4, p2)
    d3, d4 = orient(p1, p2, p3), orient(p1, p2, p4)
    return ((d1 > 0) != (d2 > 0)) and ((d3 > 0) != (d4 > 0)) and d1 != 0 and d2 != 0 and d3 != 0 and d4 != 0


def _point_segment_km(plat: float, plng: float, a: Sequence[float], b: Sequence[float]) -> float:
    """Distance (km) from a point to segment a-b, points as [lat, lng] (local flat projection)."""
    kx = 111.32 * math.cos(math.radians(plat))
    ky = 111.32
    ax, ay = (a[1] - plng) * kx, (a[0] - plat) * ky
    bx, by = (b[1] - plng) * kx, (b[0] - plat) * ky
    dx, dy = bx - ax, by - ay
    seg2 = dx * dx + dy * dy
    t = 0.0 if seg2 == 0 else max(0.0, min(1.0, -(ax * dx + ay * dy) / seg2))
    return math.hypot(ax + t * dx, ay + t * dy)


@dataclass(frozen=True)
class Hazard:
    """Either an area (`polygon`, [lng, lat] ring) or a point/polyline hazard (`points`, [lat, lng])."""

    id: str
    name: str = ""
    polygon: Tuple[Tuple[float, float], ...] = ()
    points: Tuple[Tuple[float, float], ...] = ()
    radius_km: float = POINT_HAZARD_RADIUS_KM

    def contains(self, lat: float, lng: float) -> bool:
        if self.polygon:
            return point_in_polygon(lat, lng, [list(p) for p in self.polygon])
        return any(haversine_km(lat, lng, p[0], p[1]) <= self.radius_km for p in self.points)

    def crossed_by(self, path: Sequence[Sequence[float]]) -> bool:
        """True if the road path ([lat, lng] vertices) touches this hazard anywhere (vertices and segments)."""
        if not path:
            return False
        if self.polygon:
            poly = [list(p) for p in self.polygon]
            if any(point_in_polygon(pt[0], pt[1], poly) for pt in path):
                return True
            edges = list(zip(poly, poly[1:] + poly[:1]))
            for a, b in zip(path, path[1:]):
                pa, pb = (a[1], a[0]), (b[1], b[0])
                if any(_seg_intersect(pa, pb, e0, e1) for e0, e1 in edges):
                    return True
            return False
        for hp in self.points:
            if len(path) == 1:
                if haversine_km(path[0][0], path[0][1], hp[0], hp[1]) <= self.radius_km:
                    return True
                continue
            if any(_point_segment_km(hp[0], hp[1], a, b) <= self.radius_km for a, b in zip(path, path[1:])):
                return True
        return False

    def drives_into(self, path: Sequence[Sequence[float]], origin: LatLng) -> bool:
        """Point hazard only: the path, which starts inside the hazard's radius, heads on towards it
        (it gets closer to it than the origin already is) instead of leaving it."""
        if self.polygon or not self.points or len(path) < 2:
            return False
        start = min(haversine_km(origin[0], origin[1], p[0], p[1]) for p in self.points)
        closest = min(_point_segment_km(p[0], p[1], a, b) for p in self.points for a, b in zip(path, path[1:]))
        return closest < start - POINT_LEAVE_MARGIN_KM

    def bounds(self) -> Tuple[LatLng, float]:
        """(center [lat, lng], radius_km) of a circle covering the hazard."""
        if self.polygon:
            lats = [p[1] for p in self.polygon]
            lngs = [p[0] for p in self.polygon]
            c = (sum(lats) / len(lats), sum(lngs) / len(lngs))
            return c, max(haversine_km(c[0], c[1], p[1], p[0]) for p in self.polygon)
        c = (sum(p[0] for p in self.points) / len(self.points), sum(p[1] for p in self.points) / len(self.points))
        return c, max(haversine_km(c[0], c[1], p[0], p[1]) for p in self.points) + self.radius_km


def polygon_hazard(hid: str, ring_lnglat: List[List[float]], name: str = "") -> Hazard:
    return Hazard(id=hid, name=name or hid, polygon=tuple((p[0], p[1]) for p in ring_lnglat))


def _densify_points(pts: List[LatLng], max_km: float = 0.1) -> List[LatLng]:
    out: List[LatLng] = []
    for i, p in enumerate(pts):
        out.append(p)
        if i == len(pts) - 1:
            break
        d = haversine_km(p[0], p[1], pts[i + 1][0], pts[i + 1][1])
        n = min(200, int(d / max_km))
        for k in range(1, n + 1):
            f = k / (n + 1)
            out.append((p[0] + (pts[i + 1][0] - p[0]) * f, p[1] + (pts[i + 1][1] - p[1]) * f))
    return out


def hazards_from_world(world: Optional[Dict[str, Any]]) -> List[Hazard]:
    """Live hazards from the simulator's world snapshot: affected regions (areas), blocked roads
    and collapsed bridges (point hazards). Vehicle faults and site entry are not road hazards."""
    out: List[Hazard] = []
    if not world:
        return out
    for r in world.get("affected_regions") or []:
        ring = r.get("ring") or []
        if len(ring) >= 4:
            out.append(polygon_hazard(str(r.get("id", "region")), [[p[1], p[0]] for p in ring], str(r.get("name") or r.get("id") or "region")))
    for d in world.get("disruptions") or []:
        if d.get("status", "active") != "active" or d.get("kind") not in ("road_blocked", "bridge_collapsed"):
            continue
        raw = d.get("geometry") or ([d["latlng"]] if d.get("latlng") else [])
        refs: List[LatLng] = [(float(p[0]), float(p[1])) for p in raw]
        if not refs:
            continue
        out.append(Hazard(id=str(d.get("id", "disruption")), name=str(d.get("note") or d.get("kind")), points=tuple(_densify_points(refs))))
    return out


def blocking_hazards(
    geometry: Sequence[Sequence[float]],
    origin: LatLng,
    dest: LatLng,
    hazards: Iterable[Hazard],
) -> List[Hazard]:
    """Hazards that block this route: the road path crosses them and neither endpoint is inside."""
    out: List[Hazard] = []
    for h in hazards:
        if h.contains(dest[0], dest[1]):
            continue  # going into a hazard is never blocked by it
        if h.contains(origin[0], origin[1]):
            # Leaving a hazard is never blocked, unless it is a point hazard the vehicle is stopped at and the
            # route drives on towards it.
            if h.drives_into(geometry, origin):
                out.append(h)
            continue
        if h.crossed_by(geometry):
            out.append(h)
    return out


# --------------------------------------------------------------------------- OSRM


async def osrm_routes(
    waypoints: Sequence[LatLng],
    alternatives: bool = False,
    client: Optional[httpx.AsyncClient] = None,
    heading: Optional[float] = None,
) -> List[RouteResult]:
    """Routes through the waypoints, in OSRM's order (fastest first). [] on any failure.
    Distance and duration are OSRM's own."""
    coords = ";".join(f"{lng},{lat}" for lat, lng in waypoints)
    # continue_straight=false: a route may U-turn at a waypoint. OSRM's car profile forbids it by default, which made a
    # detour that has to turn round (back away from a blockage, then around it) impossible to find.
    params = {"overview": "full", "geometries": "geojson", "continue_straight": "false"}
    if alternatives and len(waypoints) == 2:
        params["alternatives"] = "3"
    if heading is not None:
        # The vehicle is already moving: the route must leave the first waypoint within +-70 degrees of its heading,
        # so it cannot start by turning round on the spot. Later waypoints are unconstrained.
        params["bearings"] = ";".join([f"{int(round(heading)) % 360},70"] + [""] * (len(waypoints) - 1))
    own = client is None
    c = client or httpx.AsyncClient(timeout=15.0)
    try:
        resp = await c.get(f"{OSRM_BASE}/{coords}", params=params)
        if resp.status_code != 200:
            return []
        data = resp.json()
        if data.get("code") != "Ok":
            return []
        out: List[RouteResult] = []
        for r in data.get("routes") or []:
            geom = [[p[1], p[0]] for p in r["geometry"]["coordinates"]]
            if len(geom) >= 2:
                out.append((r["distance"] / 1000.0, r["duration"] / 60.0, geom))
        return out
    except Exception as exc:
        logger.warning("OSRM request failed: %s", exc)
        return []
    finally:
        if own:
            await c.aclose()


async def get_route(
    from_lat: float,
    from_lng: float,
    to_lat: float,
    to_lng: float,
    blocked_polygons: List[List[List[float]]] | None = None,
) -> Tuple[float, float, List[List[float]], bool]:
    """Returns (distance_km, duration_min, geometry [[lat,lng],...], blocked_warning).

    `blocked_polygons` are [[lng, lat], ...] rings. blocked_warning follows the blocking rule:
    the road path crosses a polygon while both endpoints are outside it."""
    hazards = [polygon_hazard(f"zone-{i}", poly) for i, poly in enumerate(blocked_polygons or [])]
    origin, dest = (from_lat, from_lng), (to_lat, to_lng)

    routes = await osrm_routes([origin, dest])
    if routes:
        dist_km, dur_min, geometry = routes[0]
        return dist_km, dur_min, geometry, bool(blocking_hazards(geometry, origin, dest, hazards))

    logger.warning("OSRM unavailable, using straight-line estimate")
    dist = haversine_km(from_lat, from_lng, to_lat, to_lng)
    dur = dist / 40.0 * 60.0  # ~40 km/h average
    geometry = [[from_lat, from_lng], [to_lat, to_lng]]
    return dist, dur, geometry, bool(blocking_hazards(geometry, origin, dest, hazards))


# --------------------------------------------------------------------------- clean detour


@dataclass
class Candidate:
    distance_km: float
    duration_min: float
    geometry: List[List[float]]
    vias: List[LatLng]
    blocking: List[Hazard]


@dataclass
class DetourSearch:
    route: Optional[Candidate]
    reason: str
    requests: int = 0
    avoided: List[str] = field(default_factory=list)


def _progress(origin: LatLng, dest: LatLng, p: LatLng) -> float:
    """Position of p along origin->dest (planar projection), used to order via-points."""
    kx = math.cos(math.radians(origin[0]))
    dx, dy = (dest[1] - origin[1]) * kx, dest[0] - origin[0]
    px, py = (p[1] - origin[1]) * kx, p[0] - origin[0]
    n = dx * dx + dy * dy
    return 0.0 if n == 0 else (px * dx + py * dy) / n


def _via_candidates(h: Hazard, origin: LatLng, dest: LatLng, hazards: List[Hazard], per_side: int = 3) -> List[LatLng]:
    """Points on a ring around the hazard that are themselves outside every hazard, best few per side
    of the origin->dest line (so the search tries going round both ways)."""
    (clat, clng), rad = h.bounds()
    ring_km = rad + VIA_CLEARANCE_KM
    dlat = ring_km / 111.32
    dlng = ring_km / (111.32 * max(0.2, math.cos(math.radians(clat))))
    sides: Dict[int, List[Tuple[float, LatLng]]] = {1: [], -1: []}
    for k in range(16):
        a = 2 * math.pi * k / 16
        p = (clat + dlat * math.sin(a), clng + dlng * math.cos(a))
        if any(hz.contains(p[0], p[1]) for hz in hazards):
            continue
        cross = (dest[1] - origin[1]) * (p[0] - origin[0]) - (dest[0] - origin[0]) * (p[1] - origin[1])
        detour = haversine_km(origin[0], origin[1], p[0], p[1]) + haversine_km(p[0], p[1], dest[0], dest[1])
        sides[1 if cross >= 0 else -1].append((detour, p))
    out: List[LatLng] = []
    for s in sides.values():
        out.extend(p for _, p in sorted(s)[:per_side])
    return out


async def find_clean_route(
    origin: LatLng,
    dest: LatLng,
    hazards: Sequence[Hazard],
    heading: Optional[float] = None,
) -> DetourSearch:
    """Fastest hazard-free OSRM route from origin to dest.

    1. OSRM direct route + its alternatives. 2. If none is clean, routes through via-points placed
    around the hazards that block the best candidates (up to VIA_ROUNDS rounds). Every returned
    route is re-checked against the hazards with the blocking rule; distance/time are OSRM's."""
    hazards = list(hazards)
    pool: List[Candidate] = []
    seen: set = set()
    requests = 0

    def add(routes: List[RouteResult], vias: List[LatLng]) -> None:
        for dist, dur, geom in routes:
            key = (round(dist, 3), round(dur, 3), len(geom))
            if key in seen:
                continue
            seen.add(key)
            pool.append(Candidate(dist, dur, geom, list(vias), blocking_hazards(geom, origin, dest, hazards)))

    def best_clean() -> Optional[Candidate]:
        clean = [c for c in pool if not c.blocking]
        return min(clean, key=lambda c: c.duration_min) if clean else None

    def finish(c: Candidate, orig_blockers: List[Hazard]) -> DetourSearch:
        return DetourSearch(c, "ok", requests, [h.name or h.id for h in orig_blockers])

    async with httpx.AsyncClient(timeout=15.0) as client:
        sem = asyncio.Semaphore(OSRM_CONCURRENCY)

        async def fetch(wps: List[LatLng], alt: bool = False) -> List[RouteResult]:
            async with sem:
                if heading is not None:  # only then, so a plain search calls osrm_routes exactly as before
                    return await osrm_routes(wps, alt, client, heading=heading)
                return await osrm_routes(wps, alt, client)

        requests += 1
        direct = await fetch([origin, dest], True)
        if not direct:
            return DetourSearch(None, "routing service returned no route", requests)
        add(direct, [])
        # what the plain fastest route ran into (for the explanation)
        orig_blockers = list(pool[0].blocking)
        found = best_clean()
        if found:
            return finish(found, orig_blockers)
        if not hazards:
            return DetourSearch(None, "no route", requests)

        tried: set = set()
        for _ in range(VIA_ROUNDS):
            base = sorted(pool, key=lambda c: (len(c.blocking), c.duration_min))[:2]
            plans: List[Tuple[float, List[LatLng]]] = []
            for cand in base:
                for h in cand.blocking:
                    for p in _via_candidates(h, origin, dest, hazards):
                        vias = sorted(cand.vias + [p], key=lambda v: _progress(origin, dest, v))
                        key = tuple((round(v[0], 5), round(v[1], 5)) for v in vias)
                        if key in tried:
                            continue
                        tried.add(key)
                        est = sum(haversine_km(a[0], a[1], b[0], b[1]) for a, b in zip([origin] + vias, vias + [dest]))
                        plans.append((est, vias))
            if not plans:
                break
            plans.sort(key=lambda x: x[0])
            chosen = plans[:MAX_VIA_REQUESTS_PER_ROUND]
            requests += len(chosen)
            results = await asyncio.gather(*(fetch([origin] + vias + [dest]) for _, vias in chosen))
            for (_, vias), routes in zip(chosen, results):
                add(routes[:1], vias)
            found = best_clean()
            if found:
                return finish(found, orig_blockers)

    return DetourSearch(None, f"no hazard-free route after {requests} OSRM requests", requests,
                        [h.name or h.id for h in orig_blockers])
