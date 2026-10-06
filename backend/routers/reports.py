"""Citizen report endpoints (public)."""

import asyncio
import math
from typing import List, Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field, field_validator

from rate_limit import limiter
from services import citizen_db
from services.bus import bus
from services.store import run_store
from services.reports import iso, now_utc, report_store

router = APIRouter(prefix="/reports", tags=["reports"])


class ReportSubmission(BaseModel):
    text: str = Field(min_length=10, max_length=500)
    lat: float
    lng: float
    accuracy_m: Optional[float] = Field(default=None, ge=0, le=1_000_000)
    location_text: str = Field(default="", max_length=200)
    need_type: str = Field(default="other", max_length=40)
    vulnerable: List[str] = Field(default_factory=list, max_length=20)
    people_count: int = Field(default=1, ge=1, le=10_000)
    is_own_location: bool = True
    reporter_lat: Optional[float] = None
    reporter_lng: Optional[float] = None
    contact: Optional[str] = Field(default=None, max_length=40)
    website: Optional[str] = Field(default=None, max_length=500)  # honeypot

    @field_validator("lat", "reporter_lat")
    @classmethod
    def _check_lat(cls, v: Optional[float]) -> Optional[float]:
        if v is not None and (not math.isfinite(v) or not -90 <= v <= 90):
            raise ValueError("latitude must be between -90 and 90")
        return v

    @field_validator("lng", "reporter_lng")
    @classmethod
    def _check_lng(cls, v: Optional[float]) -> Optional[float]:
        if v is not None and (not math.isfinite(v) or not -180 <= v <= 180):
            raise ValueError("longitude must be between -180 and 180")
        return v

    @field_validator("text")
    @classmethod
    def _text_not_blank(cls, v: str) -> str:
        if len(v.strip()) < 10:
            raise ValueError("text must be at least 10 characters")
        return v.strip()

    @field_validator("vulnerable")
    @classmethod
    def _vulnerable_items(cls, v: List[str]) -> List[str]:
        if any(len(x) > 40 for x in v):
            raise ValueError("vulnerable entries must be at most 40 characters")
        return v


@router.post("")
@limiter.limit("10/minute")
async def create_report(body: ReportSubmission, request: Request):
    if body.website and body.website.strip():
        # Honeypot tripped: look successful, store and broadcast nothing.
        return {"token": "hp_" + now_utc().strftime("%H%M%S%f"), "status": "received", "created_at": iso(now_utc())}

    submission = body.model_dump(mode="json", exclude={"website"})
    rec = report_store.add(submission, run_id=run_store.active_run_id)
    bus.publish("report.new", rec)
    return {"token": rec["token"], "status": "received", "created_at": rec["created_at"]}


@router.get("")
async def list_reports(all: bool = False):
    """Real reports come from the database the citizens write to; reports posted straight to this API
    (simulator / demo) come from the local store. Oldest first, like the local store: the dashboard prepends."""
    local = report_store.list(active_run_id=run_store.active_run_id, all_runs=all)
    if not citizen_db.configured():
        return local
    rows = await asyncio.to_thread(citizen_db.fetch_recent)  # newest first
    return [citizen_db.to_citizen_report(r) for r in reversed(rows)] + local


@router.get("/{token}")
async def get_report(token: str):
    rec = report_store.get(token)
    if not rec:
        raise HTTPException(404, "Report not found")
    return report_store.status_view(rec)
