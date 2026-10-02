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
    return load_json("hospitals.json")


def load_inventory() -> List[Dict[str, Any]]:
    return load_json("inventory.json")


def load_scenario() -> Dict[str, Any]:
    return load_json("scenario.json")


def load_blocked_zones() -> List[List[List[float]]]:
    geo = load_json("blocked_zones.geojson")
    polygons: List[List[List[float]]] = []
    for feature in geo.get("features", []):
        geom = feature.get("geometry", {})
        if geom.get("type") == "Polygon":
            polygons.append(geom["coordinates"][0])
    return polygons


def parse_report(raw: Dict[str, Any]) -> Dict[str, Any]:
    ts = raw.get("timestamp")
    if isinstance(ts, str):
        raw = {**raw, "timestamp": datetime.fromisoformat(ts.replace("Z", "+00:00"))}
    return raw
