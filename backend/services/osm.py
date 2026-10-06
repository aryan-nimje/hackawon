"""City layers from OpenStreetMap: Nominatim (bounding box) + Overpass (facilities), cleaned.

Used by `scripts/fetch_city_data.py` (run once, saved as JSON) and by `GET /layers` for cities
that are not cached yet. Depot stock and flood zones are NOT in OSM, so they are generated here.
The pure helpers (`clean_*`, `build_layers`) do no network I/O and are unit-tested.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import math
import re
from typing import Any, Dict, List, Optional

import httpx

logger = logging.getLogger(__name__)

NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
NOMINATIM_REVERSE_URL = "https://nominatim.openstreetmap.org/reverse"
# The public Overpass servers rate-limit and time out often (HTTP 429 / 504), so every request is tried on
# each mirror in turn, twice over, before giving up.
OVERPASS_URLS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
]
USER_AGENT = "disaster-response-demo/1.0 (decision-support demo)"
RETRY_STATUSES = {429, 500, 502, 503, 504}
OVERPASS_ROUNDS = 2
PER_REQUEST_TIMEOUT = 40.0
FETCH_DEADLINE = 125.0  # the browser gives up at 150 s; answer before that with a real error
# Nominatim returns the whole administrative area (a district can be 100 km wide). Overpass would time out on it,
# and a response plan only needs the urban core, so the box is capped to this many degrees (~28 km) each side of the centre.
MAX_HALF_SPAN_DEG = 0.25

MAX_HOSPITALS = 25
MAX_BRIDGES = 12
MAX_STATIONS = 8
DEDUPE_RADIUS_KM = 0.15

# Larger number = more important road, kept first when capping bridges.
HIGHWAY_RANK = {"motorway": 5, "trunk": 4, "primary": 3, "secondary": 2}


class OsmError(Exception):
    """An OpenStreetMap service (Nominatim / Overpass) could not answer. `detail` is safe to show the user."""

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


_DISTRICT_SUFFIX = re.compile(
    r"\s+(district|division|taluka|taluk|tehsil|tahsil|subdivision|metropolitan region|municipal corporation)$", re.I
)


def clean_city_name(name: str) -> str:
    """'Mumbai City District' -> 'Mumbai City'. Reverse geocoding sometimes only knows the administrative area a
    point is in; the suffix makes Nominatim return the area (or nothing) instead of the city. 'Kansas City' is kept."""
    n = (name or "").strip()
    while True:
        m = _DISTRICT_SUFFIX.sub("", n).strip()
        if m == n or not m:
            return n
        n = m


def clamp_bbox(bbox: List[float], center: List[float], half_span: float = MAX_HALF_SPAN_DEG) -> List[float]:
    """[south, west, north, east] limited to `half_span` degrees around `center`."""
    s, w, n, e = bbox
    lat, lng = center
    return [round(max(s, lat - half_span), 5), round(max(w, lng - half_span), 5),
            round(min(n, lat + half_span), 5), round(min(e, lng + half_span), 5)]


def haversine_km(a_lat: float, a_lng: float, b_lat: float, b_lng: float) -> float:
    r = 6371.0
    p1, p2 = math.radians(a_lat), math.radians(b_lat)
    dp = p2 - p1
    dl = math.radians(b_lng - a_lng)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(h))


def _norm(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", name.lower()).strip()


def _point(el: Dict[str, Any]) -> Optional[tuple]:
    if "lat" in el and "lon" in el:
        return el["lat"], el["lon"]
    c = el.get("center")
    if c and "lat" in c and "lon" in c:
        return c["lat"], c["lon"]
    return None


def _jitter(key: str, lo: int, hi: int) -> int:
    """Deterministic pseudo-random int in [lo, hi] from a string (stable across runs)."""
    h = int(hashlib.sha1(key.encode()).hexdigest()[:8], 16)
    return lo + h % (hi - lo + 1)


def estimate_beds(tags: Dict[str, str], key: str) -> tuple:
    """(beds, source). Use OSM's `beds` tag when present, else a number by hospital size."""
    raw = tags.get("beds")
    if raw:
        m = re.match(r"\s*(\d{1,5})", raw)
        if m and int(m.group(1)) > 0:
            return int(m.group(1)), "osm"
    name = (tags.get("name") or "").lower()
    if re.search(r"medical college|general|civil|district|government|sassoon|memorial", name):
        lo, hi = 40, 70  # large public / teaching hospital
    elif tags.get("emergency") == "yes" or re.search(r"multi|super|institute|research", name):
        lo, hi = 22, 40  # mid-size with emergency department
    elif re.search(r"clinic|nursing|maternity|dispensary|polyclinic", name):
        lo, hi = 6, 14  # small
    else:
        lo, hi = 12, 28
    return _jitter(key, lo, hi), "generated"


def clean_hospitals(elements: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Drop unnamed, merge duplicates (same name or within 150 m), keep the biggest."""
    cands: List[Dict[str, Any]] = []
    for el in elements:
        tags = el.get("tags") or {}
        name = (tags.get("name:en") or tags.get("name") or "").strip()
        pt = _point(el)
        if not name or not pt:
            continue
        key = f"{el.get('type', 'x')}{el.get('id', name)}"
        beds, src = estimate_beds({**tags, "name": name}, key)
        spec = [s.strip().replace("_", " ") for s in (tags.get("healthcare:speciality") or "").split(";") if s.strip()]
        if not spec:
            spec = ["general", "emergency"] if tags.get("emergency") == "yes" else ["general"]
        cands.append({"name": name, "lat": round(pt[0], 5), "lng": round(pt[1], 5), "beds": beds,
                      "beds_source": src, "specialties": spec[:4]})
    cands.sort(key=lambda h: (-h["beds"], h["name"]))  # biggest first, so it wins a dedupe
    kept: List[Dict[str, Any]] = []
    for h in cands:
        if any(_norm(h["name"]) == _norm(k["name"]) or haversine_km(h["lat"], h["lng"], k["lat"], k["lng"]) < DEDUPE_RADIUS_KM
               for k in kept):
            continue
        kept.append(h)
    kept = kept[:MAX_HOSPITALS]
    return [{"id": f"h{i + 1:03d}", **h, "beds_available": h["beds"], "status": "operational"} for i, h in enumerate(kept)]


def clean_bridges(elements: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Named bridges on major roads only, one per name, most important first."""
    best: Dict[str, Dict[str, Any]] = {}
    for el in elements:
        tags = el.get("tags") or {}
        name = (tags.get("name:en") or tags.get("name") or "").strip()
        geom = el.get("geometry") or []
        if not name or len(geom) < 2:
            continue
        rank = HIGHWAY_RANK.get(tags.get("highway", ""), 0)
        if rank == 0:
            continue
        key = _norm(name)
        length = haversine_km(geom[0]["lat"], geom[0]["lon"], geom[-1]["lat"], geom[-1]["lon"])
        if key not in best or (rank, length) > (best[key]["rank"], best[key]["length"]):
            best[key] = {"name": name, "rank": rank, "length": length,
                         "geometry": [[round(geom[0]["lat"], 5), round(geom[0]["lon"], 5)],
                                      [round(geom[-1]["lat"], 5), round(geom[-1]["lon"], 5)]]}
    top = sorted(best.values(), key=lambda b: (-b["rank"], -b["length"], b["name"]))[:MAX_BRIDGES]
    return [{"id": f"br-{i + 1:02d}", "name": b["name"], "geometry": b["geometry"]} for i, b in enumerate(top)]


def clean_stations(elements: List[Dict[str, Any]], prefix: str) -> List[Dict[str, Any]]:
    seen: List[Dict[str, Any]] = []
    for el in elements:
        tags = el.get("tags") or {}
        name = (tags.get("name:en") or tags.get("name") or "").strip()
        pt = _point(el)
        if not name or not pt:
            continue
        if any(_norm(name) == _norm(s["name"]) or haversine_km(pt[0], pt[1], s["lat"], s["lng"]) < DEDUPE_RADIUS_KM for s in seen):
            continue
        seen.append({"name": name, "lat": round(pt[0], 5), "lng": round(pt[1], 5)})
    return [{"id": f"{prefix}{i + 1:03d}", **s} for i, s in enumerate(seen[:MAX_STATIONS])]


def _rect(lat1: float, lng1: float, lat2: float, lng2: float) -> List[List[float]]:
    return [[lat1, lng1], [lat1, lng2], [lat2, lng2], [lat2, lng1], [lat1, lng1]]


def generate_depots(center: List[float]) -> List[Dict[str, Any]]:
    """Four simulated relief depots around the centre (OSM has no depot stock)."""
    lat, lng = center
    spec = [
        ("w001", "Municipal Relief Warehouse", "warehouse", -0.020, 0.005, {"food_meals": 5000, "water_bottles": 8000, "blankets": 1200, "vehicles": 12}),
        ("w002", "Red Cross Supply Depot", "ngo", 0.010, -0.010, {"food_meals": 2000, "water_bottles": 4000, "medicine_kits": 500, "vehicles": 8}),
        ("w003", "Disaster Response Force Staging Area", "government", 0.105, -0.055, {"food_meals": 10000, "water_bottles": 15000, "medicine_kits": 800, "vehicles": 25, "boats": 6}),
        ("w004", "Community Relief Centre", "community", -0.013, -0.050, {"food_meals": 800, "water_bottles": 1500, "blankets": 300, "vehicles": 3}),
    ]
    return [{"id": i, "name": f"{n} (simulated)", "lat": round(lat + dy, 5), "lng": round(lng + dx, 5), "type": t, "stock": s}
            for i, n, t, dy, dx, s in spec]


def generate_flood_zones(center: List[float], bridges: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Simulated flood zones: beside the bridges nearest the centre (rivers), else fixed offsets."""
    lat, lng = center
    anchors = [b["geometry"][0] for b in sorted(bridges, key=lambda b: haversine_km(lat, lng, *b["geometry"][0]))[:3]]
    fallback = [[lat - 0.010, lng - 0.012], [lat + 0.012, lng + 0.015], [lat - 0.030, lng - 0.040]]
    while len(anchors) < 3:
        anchors.append(fallback[len(anchors)])
    reasons = ["River overtopping", "Deep water on low-lying roads", "Street flooding 3ft+"]
    return [{"id": f"flood-{i + 1}", "name": f"Low-lying zone {i + 1} (SIMULATED)", "reason": reasons[i],
             "ring": _rect(a[0] - 0.003, a[1] - 0.004, a[0] + 0.003, a[1] + 0.004)} for i, a in enumerate(anchors)]


def build_layers(city: Dict[str, Any], facilities: List[Dict[str, Any]], bridge_ways: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Assemble the /layers payload from raw Overpass elements. `city` = {name, slug, center, zoom, bbox}."""
    by = lambda k, v: [e for e in facilities if (e.get("tags") or {}).get(k) == v]  # noqa: E731
    hospitals = clean_hospitals(by("amenity", "hospital"))
    bridges = clean_bridges(bridge_ways)
    return {
        "city": city,
        "source": "osm",
        "hospitals": hospitals,
        "bridges": bridges,
        "fire_stations": clean_stations(by("amenity", "fire_station"), "f"),
        "police_stations": clean_stations(by("amenity", "police"), "p"),
        "depots": generate_depots(city["center"]),
        "flood_zones": generate_flood_zones(city["center"], bridges),
    }


# ───────────────────────── network ─────────────────────────
def _status_detail(service: str, status: int) -> str:
    hint = {429: "rate limited", 504: "timed out", 503: "overloaded", 502: "bad gateway", 403: "access refused"}.get(status, "error")
    return f"{service} returned HTTP {status} ({hint})"


async def _nominatim(client: httpx.AsyncClient, url: str, params: Dict[str, Any]) -> Any:
    """GET from Nominatim; one retry after a short pause on 429/5xx. Raises OsmError."""
    last = "Nominatim did not answer"
    for attempt in range(2):
        try:
            r = await client.get(url, params=params, headers={"User-Agent": USER_AGENT, "Accept-Language": "en"})
        except httpx.HTTPError as exc:
            last = f"Nominatim unreachable ({type(exc).__name__})"
        else:
            if r.status_code < 400:
                return r.json()
            last = _status_detail("Nominatim", r.status_code)
            if r.status_code not in RETRY_STATUSES:
                break
        if attempt == 0:
            await asyncio.sleep(1.5)
    raise OsmError(last)


async def geocode_city(client: httpx.AsyncClient, name: str) -> Dict[str, Any]:
    """City centre + bounding box from Nominatim. Prefers a real city/town match over an administrative area."""
    name = clean_city_name(name)
    rows: List[Dict[str, Any]] = []
    for extra in ({"featuretype": "city"}, {}):  # city-level first; any place if Nominatim has no city by that name
        rows = await _nominatim(client, NOMINATIM_URL, {"q": name, "format": "jsonv2", "limit": 1, **extra})
        if rows:
            break
    if not rows:
        raise ValueError(f"City not found: {name}")
    row = rows[0]
    s, n, w, e = (float(x) for x in row["boundingbox"])  # Nominatim order: south, north, west, east
    center = [round(float(row["lat"]), 5), round(float(row["lon"]), 5)]
    short = row.get("display_name", name).split(",")[0].strip() or name
    return {
        "name": f"{short} (Simulated)",
        "slug": slugify(name),
        "center": center,
        "zoom": 12,
        "bbox": clamp_bbox([s, w, n, e], center),
    }


async def reverse_city(lat: float, lng: float) -> Optional[str]:
    """Name of the city around a point (English), or None. Used by the backend to pick the city of a real incident."""
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            d = await _nominatim(client, NOMINATIM_REVERSE_URL,
                                 {"format": "jsonv2", "zoom": 10, "addressdetails": 1, "lat": lat, "lon": lng})
    except OsmError as exc:
        logger.warning("Reverse geocode (%s, %s) failed: %s", lat, lng, exc.detail)
        return None
    a = (d or {}).get("address") or {}
    name = a.get("city") or a.get("town") or a.get("municipality") or a.get("village") \
        or a.get("city_district") or a.get("state_district") or a.get("county")
    return clean_city_name(name) if name else None


async def _overpass(client: httpx.AsyncClient, query: str) -> List[Dict[str, Any]]:
    """POST an Overpass query, failing over between mirrors and retrying 429/5xx. Raises OsmError."""
    last = "Overpass did not answer"
    for rnd in range(OVERPASS_ROUNDS):
        for url in OVERPASS_URLS:
            host = url.split("/")[2]
            try:
                r = await client.post(url, data={"data": query}, headers={"User-Agent": USER_AGENT}, timeout=PER_REQUEST_TIMEOUT)
            except httpx.HTTPError as exc:  # timeout, connection reset, DNS ...
                last = f"{host} unreachable ({type(exc).__name__})"
                continue
            if r.status_code < 400:
                try:
                    return r.json().get("elements", [])
                except ValueError:  # Overpass sometimes answers 200 with an HTML/XML error body when overloaded
                    last = f"{host} sent an unreadable answer"
                    continue
            last = _status_detail(host, r.status_code)
            logger.info("Overpass %s", last)
        if rnd < OVERPASS_ROUNDS - 1:
            await asyncio.sleep(3.0)
    raise OsmError(last)


async def _fetch(name: str) -> Dict[str, Any]:
    async with httpx.AsyncClient(timeout=PER_REQUEST_TIMEOUT) as client:
        city = await geocode_city(client, name)
        s, w, n, e = city["bbox"]
        box = f"({s},{w},{n},{e})"
        facilities = await _overpass(
            client,
            f'[out:json][timeout:60];(nwr["amenity"="hospital"]{box};nwr["amenity"="fire_station"]{box};nwr["amenity"="police"]{box};);out center tags;',
        )
        try:  # bridges only name the generated flood zones; a city is still usable without them
            bridges = await _overpass(
                client,
                f'[out:json][timeout:60];way["bridge"="yes"]["highway"~"^(motorway|trunk|primary|secondary)$"]["name"]{box};out geom tags;',
            )
        except OsmError as exc:
            logger.warning("Bridges for %r unavailable (%s); continuing without them", name, exc.detail)
            bridges = []
    return build_layers(city, facilities, bridges)


async def fetch_city_layers(name: str, timeout: float = FETCH_DEADLINE) -> Dict[str, Any]:
    """Nominatim + Overpass for one city. Raises ValueError (no such place) or OsmError (services unavailable)."""
    try:
        return await asyncio.wait_for(_fetch(name), timeout)
    except asyncio.TimeoutError as exc:
        raise OsmError("OpenStreetMap took too long to answer") from exc


def slugify(name: str) -> str:
    """'Pune, Maharashtra (Simulated)' -> 'pune'."""
    first = re.split(r"[,(]", name.strip(), maxsplit=1)[0]
    return re.sub(r"[^a-z0-9]+", "-", first.lower()).strip("-") or "city"
