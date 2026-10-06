"""Small geometry helpers for signal areas (CAP polygons are "lat,lng lat,lng ..." in WGS84)."""

from __future__ import annotations

import math
from typing import List, Optional, Tuple

Ring = List[List[float]]  # [[lat, lng], ...]


def valid_latlng(lat: float, lng: float) -> bool:
    return math.isfinite(lat) and math.isfinite(lng) and -90 <= lat <= 90 and -180 <= lng <= 180


def parse_polygon(text: str) -> Optional[Ring]:
    """Parse a CAP polygon string. Returns None when it is malformed (fewer than 3 distinct points, bad numbers,
    out-of-range coordinates). The closing point, if repeated, is dropped."""
    pts: Ring = []
    for pair in (text or "").split():
        parts = pair.split(",")
        if len(parts) != 2:
            return None
        try:
            lat, lng = float(parts[0]), float(parts[1])
        except ValueError:
            return None
        if not valid_latlng(lat, lng):
            return None
        pts.append([lat, lng])
    if len(pts) > 1 and pts[0] == pts[-1]:
        pts.pop()
    return pts if len(pts) >= 3 else None


def parse_circle(text: str) -> Optional[List[float]]:
    """CAP circle: "lat,lng radius_km" -> [lat, lng, radius_km]."""
    try:
        center, radius = (text or "").split()
        lat_s, lng_s = center.split(",")
        lat, lng, r = float(lat_s), float(lng_s), float(radius)
    except ValueError:
        return None
    return [lat, lng, r] if valid_latlng(lat, lng) and math.isfinite(r) and r >= 0 else None


def ring_centroid(ring: Ring) -> Tuple[float, float]:
    """Area centroid (planar, fine at district scale); falls back to the vertex mean for degenerate rings."""
    a = cx = cy = 0.0
    n = len(ring)
    for i in range(n):
        y0, x0 = ring[i]
        y1, x1 = ring[(i + 1) % n]
        cross = x0 * y1 - x1 * y0
        a += cross
        cx += (x0 + x1) * cross
        cy += (y0 + y1) * cross
    if abs(a) < 1e-12:
        return sum(p[0] for p in ring) / n, sum(p[1] for p in ring) / n
    return cy / (3 * a), cx / (3 * a)


def circle_bbox(lat: float, lng: float, radius_km: float) -> List[float]:
    dlat = radius_km / 111.0
    dlng = radius_km / (111.0 * max(0.01, math.cos(math.radians(lat))))
    return [lat - dlat, lng - dlng, lat + dlat, lng + dlng]


def bbox_of(polygons: List[Ring], circles: List[List[float]]) -> Optional[List[float]]:
    """[south, west, north, east] covering every polygon and circle, or None when there is no geometry."""
    boxes = [[min(p[0] for p in r), min(p[1] for p in r), max(p[0] for p in r), max(p[1] for p in r)] for r in polygons]
    boxes += [circle_bbox(*c) for c in circles]
    if not boxes:
        return None
    return [min(b[0] for b in boxes), min(b[1] for b in boxes), max(b[2] for b in boxes), max(b[3] for b in boxes)]


def centroid_of(polygons: List[Ring], circles: List[List[float]]) -> Optional[Tuple[float, float]]:
    """Centroid of the largest polygon, else the first circle's centre."""
    if polygons:
        biggest = max(polygons, key=lambda r: (max(p[0] for p in r) - min(p[0] for p in r))
                      * (max(p[1] for p in r) - min(p[1] for p in r)))
        return ring_centroid(biggest)
    if circles:
        return circles[0][0], circles[0][1]
    return None


def point_in_ring(lat: float, lng: float, ring: Ring) -> bool:
    inside = False
    j = len(ring) - 1
    for i in range(len(ring)):
        yi, xi = ring[i]
        yj, xj = ring[j]
        if (yi > lat) != (yj > lat) and lng < (xj - xi) * (lat - yi) / (yj - yi) + xi:
            inside = not inside
        j = i
    return inside


def bbox_overlaps(a: List[float], b: List[float]) -> bool:
    return not (a[2] < b[0] or b[2] < a[0] or a[3] < b[1] or b[3] < a[1])
