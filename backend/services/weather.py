"""Weather service using Open-Meteo (no key required)."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional

import httpx

from config import get_settings
from state import Incident, IncidentSource, NeedType, Urgency

logger = logging.getLogger(__name__)

# Houston, TX — urban flood scenario center
DEFAULT_LAT = 29.7604
DEFAULT_LNG = -95.3698


async def fetch_weather_alert(
    lat: float = DEFAULT_LAT,
    lng: float = DEFAULT_LNG,
) -> Optional[Incident]:
    """Fetch current weather and create an alert incident if conditions are severe."""
    settings = get_settings()
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(
                "https://api.open-meteo.com/v1/forecast",
                params={
                    "latitude": lat,
                    "longitude": lng,
                    "current": "precipitation,rain,weather_code",
                    "hourly": "precipitation_probability,rain",
                    "forecast_days": 1,
                },
            )
            resp.raise_for_status()
            data = resp.json()
            current = data.get("current", {})
            precip = current.get("precipitation", 0) or 0
            rain = current.get("rain", 0) or 0
            code = current.get("weather_code", 0)

            if precip > 5 or rain > 3 or code in (65, 66, 67, 80, 81, 82):
                severity_text = "Heavy rainfall detected"
                if precip > 15:
                    severity_text = "Extreme rainfall — flood risk critical"
                elif precip > 8:
                    severity_text = "Significant rainfall — localized flooding likely"

                return Incident(
                    id="weather-alert-001",
                    text=(
                        f"[SIMULATED WEATHER ALERT] {severity_text}. "
                        f"Current precipitation: {precip}mm, rain: {rain}mm. "
                        "Urban flood conditions possible in low-lying areas."
                    ),
                    location="Greater Houston Metro",
                    lat=lat,
                    lng=lng,
                    need_type=NeedType.INFORMATION,
                    urgency=Urgency.HIGH if precip > 8 else Urgency.MEDIUM,
                    source=IncidentSource.WEATHER,
                    timestamp=datetime.utcnow(),
                    raw_metadata={"precipitation_mm": precip, "rain_mm": rain, "weather_code": code},
                )
    except Exception as exc:
        logger.warning("Weather API failed, using mock alert: %s", exc)

    if settings.effective_mock_mode:
        return Incident(
            id="weather-alert-001",
            text=(
                "[SIMULATED WEATHER ALERT] Heavy rainfall event in progress. "
                "Flash flood warning for Harris County. 80mm accumulated in 6 hours."
            ),
            location="Greater Houston Metro",
            lat=lat,
            lng=lng,
            need_type=NeedType.INFORMATION,
            urgency=Urgency.HIGH,
            source=IncidentSource.WEATHER,
            timestamp=datetime.utcnow(),
            raw_metadata={"precipitation_mm": 80, "mock": True},
        )
    return None
