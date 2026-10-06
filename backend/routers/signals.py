"""External evidence (common signals): read-only list + per-source refresh (SACHET, Open-Meteo, GDELT). No citizen data is involved."""

from __future__ import annotations

from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, HTTPException, Query, Request

from config import get_settings
from rate_limit import limiter
from services import city as city_service
from services.osm import slugify
from services import corroboration
from services.signals import gdelt, ingest, open_meteo, scoring
from services.signals.models import SignalKind, SignalSource, SignalView
from services.signals.store import signal_store

router = APIRouter(prefix="/signals", tags=["signals"])


def _view(sig, now: datetime) -> SignalView:
    return SignalView(**sig.model_dump(), active=scoring.is_in_force(sig, now), freshness=scoring.freshness(sig, now),
                      score=scoring.evidence_score(sig, now))


@router.get("", response_model=List[SignalView])
async def list_signals(
    source: Optional[SignalSource] = None,
    kind: Optional[SignalKind] = None,
    active_only: bool = True,
    city: Optional[str] = Query(default=None, max_length=200, description="City slug (cached cities only): signals whose area overlaps its box"),
    limit: int = Query(default=100, ge=1, le=500),
):
    """Newest first. `active_only` hides expired, cancelled and superseded signals. `city` leaves out signals with
    no geometry (they cannot be placed)."""
    now = datetime.utcnow()
    bbox = None
    if city:
        layers = city_service.load_cached(city_service.slugify(city))
        bbox = ((layers or {}).get("city") or {}).get("bbox")
        if not bbox:
            raise HTTPException(404, f"No cached city data for {city!r}")
    sigs = signal_store.list(source=source, kind=kind, bbox=bbox,
                             include=(lambda s: scoring.is_in_force(s, now)) if active_only else None)
    return [_view(s, now) for s in sigs[:limit]]


@router.get("/status")
async def signals_status():
    s = get_settings()
    return {
        "counts": signal_store.counts(),
        "sachet": {"enabled": s.sachet_enabled, "feeds": s.sachet_feeds, "poll_seconds": s.sachet_poll_seconds,
                   "last_refresh": ingest.last_refresh["sachet"]},
        "open_meteo": {"enabled": s.open_meteo_enabled, "cities": s.open_meteo_city_names,
                       "poll_seconds": s.open_meteo_poll_seconds, "last_refresh": ingest.last_refresh["open_meteo"]},
        "gdelt": {"enabled": s.gdelt_enabled, "cities": s.gdelt_city_names, "poll_seconds": s.gdelt_poll_seconds,
                  "last_refresh": ingest.last_refresh["gdelt"]},
    }


@router.get("/scoring")
async def scoring_constants():
    """The scoring constants in force (defaults + environment overrides)."""
    return {
        "source_trust": {src.value: scoring.source_trust(src) for src in SignalSource},
        "severity_weights": scoring.severity_weights(),
        "certainty_weights": scoring.certainty_weights(),
        "urgency_weights": scoring.urgency_weights(),
        "component_weights": scoring.component_weights(),
        "freshness_half_life_hours": scoring.freshness_half_life_hours(),
        "freshness_floor": scoring.freshness_floor(),
        "open_meteo": open_meteo.settings_in_force(),
        "gdelt": gdelt.settings_in_force(),
        "corroboration": corroboration.settings_in_force(),
    }


@router.post("/sachet/refresh")
@limiter.limit("6/minute")
async def refresh_sachet(request: Request):
    """Pull new SACHET alerts now (the poller does this on a schedule). Always 200 with a summary; `ok: false`
    means no feed could be read."""
    return await ingest.refresh_sachet()


def _require_cached_city(city: Optional[str]) -> Optional[str]:
    if city and not city_service.load_cached(slugify(city)):
        raise HTTPException(404, f"No cached city data for {city!r}")
    return city


@router.post("/open-meteo/refresh")
@limiter.limit("6/minute")
async def refresh_open_meteo(
    request: Request,
    city: Optional[str] = Query(default=None, max_length=200, description="Cached city only; default = the configured cities"),
):
    """Pull rain / river-flood / wind / thunderstorm / heat conditions now (the poller does this on a schedule).
    Always 200 with a summary; `ok: false` means no request could be read."""
    return await ingest.refresh_open_meteo(city=_require_cached_city(city))


@router.post("/gdelt/refresh")
@limiter.limit("3/minute")
async def refresh_gdelt(
    request: Request,
    city: Optional[str] = Query(default=None, max_length=200, description="Cached city only; default = the configured cities"),
):
    """Pull hazard news now (the poller does this on a schedule). GDELT allows about one request per 5 seconds, hence
    the tighter limit. Always 200 with a summary; `ok: false` means no query could be read."""
    return await ingest.refresh_gdelt(city=_require_cached_city(city))
