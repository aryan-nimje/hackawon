"""Load mock data files from backend/data/."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List

DATA_DIR = Path(__file__).resolve().parent.parent / "data"


def load_json(name: str) -> Any:
    with open(DATA_DIR / name, encoding="utf-8") as f:
        return json.load(f)


def load_reports() -> List[Dict[str, Any]]:
    return load_json("reports.json")


def load_news() -> List[Dict[str, Any]]:
    return load_json("news.json")


def load_hospitals() -> List[Dict[str, Any]]:
    """Hospitals of the default city, in the shape the agents use (`beds_available`)."""
    from services.city import get_default_layers

    out = []
    for h in get_default_layers().get("hospitals", []):
        out.append({**h, "beds_available": h.get("beds_available", h.get("beds", 0))})
    return out


def load_inventory() -> List[Dict[str, Any]]:
    """Depots of the default city, in the shape the logistics agent uses (`inventory`)."""
    from services.city import get_default_layers

    return [{**d, "inventory": dict(d.get("stock", {}))} for d in get_default_layers().get("depots", [])]


def load_scenario() -> Dict[str, Any]:
    return load_json("scenario.json")


def is_scenario_city(slug: str | None) -> bool:
    """True when `slug` is the city the shipped scenario and reports are written for (Pune), or no city is set.
    Their coordinates only make sense there, so other cities start empty and get incidents from the simulator."""
    if not slug:
        return True
    from services.osm import slugify

    return slug == slugify(str(load_scenario().get("city", "pune")))


def load_blocked_zones() -> List[List[List[float]]]:
    """Flood-zone polygons as [[lng, lat], ...] rings (GeoJSON order) for route checks."""
    from services.city import get_default_layers

    polygons: List[List[List[float]]] = []
    for z in get_default_layers().get("flood_zones", []):
        ring = z.get("ring") or []
        if len(ring) >= 4:
            polygons.append([[p[1], p[0]] for p in ring])
    return polygons


def parse_report(raw: Dict[str, Any]) -> Dict[str, Any]:
    ts = raw.get("timestamp")
    if isinstance(ts, str):
        raw = {**raw, "timestamp": datetime.fromisoformat(ts.replace("Z", "+00:00"))}
    return raw
