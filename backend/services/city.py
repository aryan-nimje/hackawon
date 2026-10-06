"""City layers (hospitals, depots, flood zones, bridges, stations) served by GET /layers.

`data/layers_<slug>.json` is the cache. Pune ships as a seed file so the demo never depends on live
Overpass; other cities are fetched on first request (if LIVE_OSM) and cached on disk.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Dict, Optional

from config import get_settings
from services.data_loader import DATA_DIR
from services.osm import OsmError, clean_city_name, fetch_city_layers, reverse_city, slugify

logger = logging.getLogger(__name__)

# City the current task is planning for (set per run by the supervisor). None = DEFAULT_CITY.
_active_slug: ContextVar[Optional[str]] = ContextVar("active_city_slug", default=None)

_cache: Dict[str, Dict[str, Any]] = {}
_fetch_lock = asyncio.Lock()


class LayersUnavailable(Exception):
    """No layers for a city: nothing cached and OpenStreetMap could not supply them. `status_code` is for HTTP."""

    def __init__(self, message: str, status_code: int = 404) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code


def _path(slug: str):
    return DATA_DIR / f"layers_{slug}.json"


def load_cached(slug: str) -> Optional[Dict[str, Any]]:
    if slug in _cache:
        return _cache[slug]
    p = _path(slug)
    if not p.exists():
        return None
    with open(p, encoding="utf-8") as f:
        data = json.load(f)
    _cache[slug] = data
    return data


def save_layers(slug: str, layers: Dict[str, Any]) -> None:
    """Write data/layers_<slug>.json atomically (temp file, then rename) so a crash never leaves a broken cache."""
    path = _path(slug)
    tmp = path.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(layers, f, indent=1)
    os.replace(tmp, path)
    _cache[slug] = layers


@contextmanager
def city_context(slug: Optional[str]):
    """Make `get_default_layers()` / `get_city()` and everything built on them (hospitals, depots, flood
    zones, weather, routing, verification) use `slug` inside this block, including tasks started in it."""
    if not slug:
        yield
        return
    token = _active_slug.set(slug)
    try:
        yield
    finally:
        _active_slug.reset(token)


def get_default_layers() -> Dict[str, Any]:
    """Layers of the city in force: the run's city (see `city_context`), else DEFAULT_CITY, else Pune."""
    layers = (
        (_active_slug.get() and load_cached(_active_slug.get()))
        or load_cached(slugify(get_settings().default_city))
        or load_cached("pune")
    )
    if layers is None:
        raise RuntimeError("No city layers found in backend/data (expected layers_pune.json)")
    return layers


def get_city() -> Dict[str, Any]:
    """{name, slug, center [lat, lng], zoom, bbox [south, west, north, east]} of the default city."""
    return get_default_layers()["city"]


def city_label() -> str:
    """Short name for messages, e.g. 'Pune'."""
    return get_city()["name"].split(",")[0].split("(")[0].strip()


def in_city_bbox(lat: float, lng: float) -> bool:
    s, w, n, e = get_city()["bbox"]
    return s <= lat <= n and w <= lng <= e


async def get_layers(city: Optional[str], refresh: bool = False) -> Dict[str, Any]:
    """Layers for `city`: the cached file if there is one, otherwise fetched from OSM and cached.

    Order: cache -> Nominatim/Overpass (saved as layers_<slug>.json) -> cache again if the fetch failed
    (matters for refresh=True). Raises LayersUnavailable with a clear message when there is neither.
    """
    settings = get_settings()
    name = clean_city_name((city or "").strip()) or settings.default_city
    slug = slugify(name)
    if not refresh:
        cached = load_cached(slug)
        if cached is not None:
            return cached
    if not settings.live_osm:
        cached = load_cached(slug)
        if cached is not None:
            return cached
        raise LayersUnavailable(
            f"No cached data for {name!r} and live fetching from OpenStreetMap is disabled (LIVE_OSM=false).", 404
        )
    async with _fetch_lock:  # one Overpass request at a time; re-check the cache inside the lock
        if not refresh and (cached := load_cached(slug)) is not None:
            return cached
        try:
            layers = await fetch_city_layers(name)
        except Exception as exc:
            logger.warning("OSM fetch for %r failed: %s", name, exc)
            cached = load_cached(slug)  # a stale cache beats nothing on refresh
            if cached is not None:
                return cached
            if isinstance(exc, ValueError):  # Nominatim found no such place
                raise LayersUnavailable(f"City {name!r} was not found on OpenStreetMap.", 404) from exc
            detail = exc.detail if isinstance(exc, OsmError) else type(exc).__name__
            raise LayersUnavailable(
                f"Could not fetch data for {name!r} from OpenStreetMap ({detail}) and there is no cached copy. "
                "Try again in a minute.",
                502,
            ) from exc
        if not layers["hospitals"]:
            cached = load_cached(slug)
            if cached is not None:
                return cached
            raise LayersUnavailable(f"OpenStreetMap has no hospitals for {name!r}, so no city data was saved.", 404)
        layers["city"]["slug"] = slug
        save_layers(slug, layers)
        return layers


def find_cached_city(lat: float, lng: float) -> Optional[str]:
    """Slug of a city already on disk whose box contains the point (no network). The default city wins ties."""
    default = slugify(get_settings().default_city)
    slugs = sorted((p.stem[len("layers_"):] for p in DATA_DIR.glob("layers_*.json")), key=lambda x: x != default)
    for slug in slugs:
        layers = load_cached(slug)
        bbox = (layers or {}).get("city", {}).get("bbox")
        if bbox and bbox[0] <= lat <= bbox[2] and bbox[1] <= lng <= bbox[3]:
            return slug
    return None


async def city_for_point(lat: float, lng: float) -> Optional[str]:
    """Slug of the city a real incident at (lat, lng) belongs to, making sure its layers exist.

    Cached cities are matched by bounding box first (instant, offline). Otherwise, when LIVE_OSM is on, the point is
    reverse-geocoded and that city is loaded like any other. Returns None when it cannot be told; the caller then
    keeps the default city, exactly as before.
    """
    slug = find_cached_city(lat, lng)
    if slug or not get_settings().live_osm:
        return slug
    name = await reverse_city(lat, lng)
    if not name:
        return None
    try:
        return (await get_layers(name))["city"].get("slug") or slugify(name)
    except LayersUnavailable as exc:
        logger.warning("No layers for %r near (%s, %s): %s", name, lat, lng, exc.message)
        return None


def effective_city_slug(run_city: Optional[str]) -> str:
    """Slug a run really plans for: its own city, else DEFAULT_CITY. Sent with `run.active` so every dashboard can
    follow it, including runs that were not started from the simulator."""
    return run_city or slugify(get_settings().default_city)
