"""GET /layers?city=<name> — map layers for any city (cached on disk; fetched from OSM on first request)."""

from fastapi import APIRouter, HTTPException, Request

from rate_limit import limiter
from services.city import LayersUnavailable, get_layers

router = APIRouter(tags=["layers"])


@router.get("/layers")
@limiter.limit("30/minute")
async def layers(request: Request, city: str | None = None, refresh: bool = False):
    try:
        return await get_layers(city, refresh=refresh)
    except LayersUnavailable as exc:
        raise HTTPException(exc.status_code, exc.message)
