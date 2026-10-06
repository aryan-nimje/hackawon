#!/usr/bin/env python3
"""Fetch a city's layers from OpenStreetMap once and save them as backend/data/layers_<slug>.json.

    cd backend
    python scripts/fetch_city_data.py --city Pune

Nominatim gives the bounding box; Overpass gives hospitals, bridges on major roads, and fire and
police stations. The result is cleaned (unnamed entries and duplicates dropped, bridges capped) and
depots + flood zones are generated around the centre. Hospital beds use OSM's `beds` tag when present,
otherwise a number by hospital size. Needs internet access to nominatim.openstreetmap.org and
overpass-api.de; the demo itself then reads only the saved JSON.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from services.city import save_layers  # noqa: E402
from services.osm import fetch_city_layers, slugify  # noqa: E402


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--city", default="Pune")
    args = ap.parse_args()
    layers = await fetch_city_layers(args.city)
    slug = slugify(args.city)
    layers["city"]["slug"] = slug
    if not layers["hospitals"]:
        print("No hospitals found; not saving.", file=sys.stderr)
        return 1
    save_layers(slug, layers)
    c = layers
    print(f"Saved data/layers_{slug}.json: {len(c['hospitals'])} hospitals "
          f"({sum(h['beds_source'] == 'osm' for h in c['hospitals'])} with OSM beds), {len(c['bridges'])} bridges, "
          f"{len(c['fire_stations'])} fire, {len(c['police_stations'])} police, "
          f"{len(c['depots'])} depots and {len(c['flood_zones'])} flood zones (generated).")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
