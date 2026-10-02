"""Routing service using public OSRM with straight-line fallback."""

from __future__ import annotations

import logging
import math
from typing import List, Tuple

import httpx

logger = logging.getLogger(__name__)

OSRM_BASE = "https://router.project-osrm.org/route/v1/driving"


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


async def get_route(
    from_lat: float,
    from_lng: float,
    to_lat: float,
    to_lng: float,
    blocked_polygons: List[List[List[float]]] | None = None,
) -> Tuple[float, float, List[List[float]], bool]:
    """
    Returns (distance_km, duration_min, geometry [[lat,lng],...], blocked_warning).
    """
    blocked = False
    if blocked_polygons:
        for poly in blocked_polygons:
            if point_in_polygon(from_lat, from_lng, poly) or point_in_polygon(
                to_lat, to_lng, poly
            ):
                blocked = True
                break

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            url = f"{OSRM_BASE}/{from_lng},{from_lat};{to_lng},{to_lat}"
            resp = await client.get(url, params={"overview": "full", "geometries": "geojson"})
            if resp.status_code == 200:
                data = resp.json()
                if data.get("code") == "Ok" and data.get("routes"):
                    route = data["routes"][0]
                    dist_km = route["distance"] / 1000.0
                    dur_min = route["duration"] / 60.0
                    coords = route["geometry"]["coordinates"]
                    geometry = [[c[1], c[0]] for c in coords]
                    if blocked_polygons and geometry:
                        for pt in geometry[:: max(1, len(geometry) // 10)]:
                            for poly in blocked_polygons:
                                if point_in_polygon(pt[0], pt[1], poly):
                                    blocked = True
                                    break
                    return dist_km, dur_min, geometry, blocked
    except Exception as exc:
        logger.warning("OSRM routing failed, using straight-line estimate: %s", exc)

    dist = haversine_km(from_lat, from_lng, to_lat, to_lng)
    dur = dist / 40.0 * 60.0  # ~40 km/h average
    geometry = [[from_lat, from_lng], [to_lat, to_lng]]
    return dist, dur, geometry, blocked
