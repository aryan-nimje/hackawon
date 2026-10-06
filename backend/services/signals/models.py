"""Common signal format: one shape for every external evidence source (SACHET alerts, GDELT news, Open-Meteo weather).

A Signal says "something outside the citizen reports reported this", with where/when/how serious, how far to trust
its source, and how long it stays relevant. It carries NO citizen data: no reporter ids, history or tracking.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class SignalSource(str, Enum):
    SACHET = "sachet"  # India's national CAP alert hub (NDMA): official alerts
    GDELT = "gdelt"  # news articles (city-level location: the city that was searched)
    OPEN_METEO = "open_meteo"  # model-based rain / river-flood / wind / thunderstorm / heat conditions


class SignalKind(str, Enum):
    OFFICIAL_ALERT = "official_alert"
    NEWS = "news"
    WEATHER = "weather"


class SignalSeverity(str, Enum):
    UNKNOWN = "unknown"
    MINOR = "minor"
    MODERATE = "moderate"
    SEVERE = "severe"
    EXTREME = "extreme"


class SignalStatus(str, Enum):
    ACTIVE = "active"
    CANCELLED = "cancelled"  # the issuer withdrew it (CAP msgType=Cancel)
    SUPERSEDED = "superseded"  # replaced by a newer update (CAP msgType=Update)


class Signal(BaseModel):
    id: str  # stable and source-prefixed, e.g. "sachet:<cap identifier>"; re-ingesting the same id updates in place
    source: SignalSource
    kind: SignalKind
    title: str
    text: str = ""  # description / body
    event: str = ""  # e.g. "Heavy Rain"
    hazards: List[str] = Field(default_factory=list)  # normalized tags: flood, rain, cyclone, landslide, ...
    severity: SignalSeverity = SignalSeverity.UNKNOWN
    urgency: str = "unknown"  # immediate | expected | future | past | unknown
    certainty: str = "unknown"  # observed | likely | possible | unlikely | unknown
    instruction: str = ""
    area: str = ""  # human-readable area description
    lat: Optional[float] = None  # centroid of the area, when the source gave geometry
    lng: Optional[float] = None
    bbox: Optional[List[float]] = None  # [south, west, north, east]
    polygons: List[List[List[float]]] = Field(default_factory=list)  # rings of [lat, lng]
    circles: List[List[float]] = Field(default_factory=list)  # [lat, lng, radius_km]
    sender: str = ""
    source_url: str = ""
    language: str = ""
    msg_type: str = "alert"  # alert | update | cancel
    references: List[str] = Field(default_factory=list)  # ids of signals this one updates/cancels
    issued_at: Optional[datetime] = None  # naive UTC everywhere
    effective_at: Optional[datetime] = None
    expires_at: Optional[datetime] = None
    fetched_at: datetime = Field(default_factory=datetime.utcnow)
    status: SignalStatus = SignalStatus.ACTIVE
    # Scoring inputs, set at normalization from configurable constants (see scoring.py). Freshness is applied at read time.
    trust: float = Field(default=0.0, ge=0.0, le=1.0)  # how far the SOURCE is trusted
    weight: float = Field(default=0.0, ge=0.0, le=1.0)  # trust x (severity, certainty, urgency), before freshness
    metadata: Dict[str, Any] = Field(default_factory=dict)  # small source-specific extras (category, response type, ...)


class SignalView(Signal):
    """A signal as served by the API: plus the live evidence score and whether it is still in force."""

    active: bool = True
    freshness: float = 1.0
    score: float = 0.0
