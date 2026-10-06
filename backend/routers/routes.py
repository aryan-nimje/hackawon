"""GET /routes/leg - one road route between two points (used by the simulator for hospital-to-hospital transfers)."""

from fastapi import APIRouter, Query, Request

from rate_limit import limiter
from services.routing import get_route

router = APIRouter(prefix="/routes", tags=["routes"])


@router.get("/leg")
@limiter.limit("120/minute")
async def leg(
    request: Request,
    from_lat: float = Query(ge=-90, le=90),
    from_lng: float = Query(ge=-180, le=180),
    to_lat: float = Query(ge=-90, le=90),
    to_lng: float = Query(ge=-180, le=180),
):
    dist, dur, geometry, _ = await get_route(from_lat, from_lng, to_lat, to_lng)
    return {"distance_km": round(dist, 2), "duration_min": round(dur, 1), "geometry": geometry}
