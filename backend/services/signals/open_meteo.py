"""Open-Meteo (free, key-less weather + GloFAS river discharge) as a signal source.

Two APIs per city:
  * forecast  -> hourly precipitation, wind gusts, temperature, weather code  => rain / wind / storm / heat signals
  * flood     -> daily river discharge (past window + forecast)               => river-flood signal

A signal exists ONLY while a configured threshold is crossed ("nothing special" produces no signal, so weather never
sits in the store at a fixed score). Each signal is keyed `open_meteo:<city>:<type>` and rewritten on every refresh;
it lives OPEN_METEO_TTL_HOURS from the last refresh, and ingest expires it early when a successful refresh no longer
sees the hazard. A failed or malformed response never expires anything.

Honest limits: this is model output (no issuing authority), so trust tops out at 0.7 and certainty is never
"observed"; GloFAS discharge is a ~5 km grid cell that may not be the city's own river; every signal carries the
city's box (that is where it was asked for), not a hazard footprint.

Thresholds are overridable from the environment (see .env.example); a malformed override is logged and ignored.
"""

from __future__ import annotations

import logging
import statistics
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Set, Tuple

import httpx

from config import get_settings
from services.signals import common, scoring
from services.signals.common import CityRef, FetchError
from services.signals.models import Signal, SignalKind, SignalSeverity, SignalSource

log = logging.getLogger("signals.open_meteo")

# IMD-style daily rainfall classes (mm / 24 h): rather heavy 35.5, heavy 64.5, very heavy 115.6, extremely heavy 204.5.
DEFAULT_RAIN_24H_MM: Dict[str, float] = {"minor": 35.5, "moderate": 64.5, "severe": 115.6, "extreme": 204.5}
DEFAULT_RAIN_1H_MM: Dict[str, float] = {"minor": 7.6, "moderate": 15.0, "severe": 30.0, "extreme": 50.0}
# Forecast peak river discharge / median of the last OPEN_METEO_FLOOD_PAST_DAYS days.
DEFAULT_FLOOD_RATIOS: Dict[str, float] = {"minor": 2.0, "moderate": 3.0, "severe": 5.0, "extreme": 8.0}
DEFAULT_WIND_GUST_KMH: Dict[str, float] = {"minor": 50.0, "moderate": 62.0, "severe": 89.0, "extreme": 118.0}
DEFAULT_HEAT_C: Dict[str, float] = {"minor": 40.0, "moderate": 43.0, "severe": 45.0, "extreme": 47.0}
THUNDER_CODES = {95: SignalSeverity.MODERATE, 96: SignalSeverity.SEVERE, 99: SignalSeverity.SEVERE}  # WMO codes

MIN_WINDOW_VALUES = 18  # of 24 hourly values needed before a rolling 24 h rainfall total is trusted
MIN_BASELINE_DAYS = 7  # past discharge values needed before a ratio means anything
RAIN_LABEL = {SignalSeverity.MINOR: "Rather heavy rain", SignalSeverity.MODERATE: "Heavy rain",
              SignalSeverity.SEVERE: "Very heavy rain", SignalSeverity.EXTREME: "Extremely heavy rain"}


class OpenMeteoParseError(ValueError):
    """The response is JSON but not a usable forecast / flood document."""


@dataclass
class OpenMeteoResult:
    signals: List[Signal] = field(default_factory=list)
    scopes_ok: Set[Tuple[str, str]] = field(default_factory=set)  # (city slug, "forecast" | "flood") that parsed fine
    requests_ok: int = 0
    requests_failed: int = 0
    malformed: int = 0
    cities: int = 0
    errors: List[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return bool(self.scopes_ok)

    def err(self, msg: str) -> None:
        if len(self.errors) < common.MAX_ERRORS_KEPT:
            self.errors.append(msg[:300])

    def summary(self) -> Dict[str, Any]:
        return {"ok": self.ok, "cities": self.cities, "requests_ok": self.requests_ok,
                "requests_failed": self.requests_failed, "malformed": self.malformed,
                "signals": len(self.signals), "errors": list(self.errors)}


# -- configuration -----------------------------------------------------------------------------------------
def thresholds() -> Dict[str, Dict[str, float]]:
    s = get_settings()
    return {
        "rain_24h_mm": common.parse_ladder("OPEN_METEO_RAIN_24H_MM", s.open_meteo_rain_24h_mm, DEFAULT_RAIN_24H_MM),
        "rain_1h_mm": common.parse_ladder("OPEN_METEO_RAIN_1H_MM", s.open_meteo_rain_1h_mm, DEFAULT_RAIN_1H_MM),
        "flood_ratios": common.parse_ladder("OPEN_METEO_FLOOD_RATIOS", s.open_meteo_flood_ratios, DEFAULT_FLOOD_RATIOS),
        "wind_gust_kmh": common.parse_ladder("OPEN_METEO_WIND_GUST_KMH", s.open_meteo_wind_gust_kmh, DEFAULT_WIND_GUST_KMH),
        "heat_c": common.parse_ladder("OPEN_METEO_HEAT_C", s.open_meteo_heat_c, DEFAULT_HEAT_C),
    }


def settings_in_force() -> Dict[str, Any]:
    """Everything configurable about this source, for GET /signals/scoring."""
    s = get_settings()
    return {
        "thresholds": thresholds(),
        "ttl_hours": ttl_hours(),
        "certain_within_hours": certain_within_hours(),
        "flood": {"enabled": s.open_meteo_flood_enabled, "past_days": flood_past_days(),
                  "horizon_days": flood_horizon_days(), "min_discharge_m3s": flood_min_discharge()},
    }


def ttl_hours() -> float:
    return common.positive_or(get_settings().open_meteo_ttl_hours, 3.0, "OPEN_METEO_TTL_HOURS")


def certain_within_hours() -> float:
    return common.positive_or(get_settings().open_meteo_certain_within_hours, 24.0, "OPEN_METEO_CERTAIN_WITHIN_HOURS")


def flood_past_days() -> int:
    d = get_settings().open_meteo_flood_past_days
    return d if MIN_BASELINE_DAYS <= d <= 92 else 30  # the API allows up to 92 past days


def flood_horizon_days() -> int:
    d = get_settings().open_meteo_flood_horizon_days
    return d if 1 <= d <= 16 else 5


def flood_min_discharge() -> float:
    v = common.as_float(get_settings().open_meteo_flood_min_discharge)
    return v if v is not None and v >= 0 else 5.0


# -- response parsing --------------------------------------------------------------------------------------
def _block(data: Any, name: str) -> Dict[str, Any]:
    if not isinstance(data, dict):
        raise OpenMeteoParseError("response is not a JSON object")
    if data.get("error"):
        raise OpenMeteoParseError(f"API error: {common.clean_text(data.get('reason'), 120)}")
    block = data.get(name)
    if not isinstance(block, dict) or not isinstance(block.get("time"), list) or not block["time"]:
        raise OpenMeteoParseError(f"no '{name}' time series")
    return block


def _times(block: Dict[str, Any]) -> List[Optional[datetime]]:
    return [common.parse_naive_utc(t) for t in block["time"]]


def _values(block: Dict[str, Any], key: str, n: int) -> List[Optional[float]]:
    """Numbers aligned to the time axis. A missing, wrongly-typed or short series is all-None (or padded with None)."""
    raw = block.get(key)
    if not isinstance(raw, list):
        return [None] * n
    vals = [common.as_float(v) for v in raw[:n]]
    return vals + [None] * (n - len(vals))


def _hours_between(later: datetime, earlier: datetime) -> float:
    return (later - earlier).total_seconds() / 3600.0


def _certainty(lead_hours: float) -> str:
    # Model output: "likely" at best, never "observed".
    return "likely" if lead_hours <= certain_within_hours() else "possible"


def _make(city: CityRef, kind: str, *, endpoint: str, title: str, text: str, event: str, hazards: List[str],
          severity: SignalSeverity, urgency: str, certainty: str, effective_at: Optional[datetime],
          now: datetime, fetched_at: datetime, metadata: Dict[str, Any]) -> Signal:
    sig = Signal(
        id=f"open_meteo:{city.slug}:{kind}", source=SignalSource.OPEN_METEO, kind=SignalKind.WEATHER,
        title=title, text=text, event=event, hazards=hazards, severity=severity, urgency=urgency, certainty=certainty,
        area=city.label, lat=city.lat, lng=city.lng, bbox=list(city.bbox), sender="Open-Meteo",
        source_url="https://open-meteo.com/", msg_type="alert", issued_at=now, effective_at=effective_at,
        expires_at=now + timedelta(hours=ttl_hours()), fetched_at=fetched_at,
        trust=scoring.source_trust(SignalSource.OPEN_METEO),
        metadata={"city": city.slug, "endpoint": endpoint, "type": kind, **metadata},
    )
    sig.weight = scoring.signal_weight(sig)
    return sig


def _peak(times: List[Optional[datetime]], vals: List[Optional[float]], lo: datetime, hi: datetime):
    """(value, time) of the highest valid value with lo <= time <= hi, or None."""
    best = None
    for t, v in zip(times, vals):
        if t is not None and v is not None and lo <= t <= hi and (best is None or v > best[0]):
            best = (v, t)
    return best


def analyze_forecast(data: Any, city: CityRef, now: datetime, fetched_at: Optional[datetime] = None) -> List[Signal]:
    """Forecast document -> rain / wind / storm / heat signals for the thresholds it crosses.
    Raises OpenMeteoParseError when the document has no usable hourly time axis."""
    fetched_at = fetched_at or now
    block = _block(data, "hourly")
    times = _times(block)
    if not any(times):
        raise OpenMeteoParseError("hourly times unreadable")
    n = len(times)
    th = thresholds()
    now_h = now.replace(minute=0, second=0, microsecond=0)
    out: List[Signal] = []

    # -- rain: worst rolling 24 h total (still-relevant windows) and worst 1 h intensity ----------------------
    precip = _values(block, "precipitation", n)
    best_total: Optional[Tuple[float, datetime]] = None  # (mm, window end)
    for i in range(23, n):
        end, start = times[i], times[i - 23]
        if end is None or start is None or _hours_between(end, start) != 23 or end < now_h - timedelta(hours=2):
            continue
        window = [v for v in precip[i - 23:i + 1] if v is not None]
        if len(window) >= MIN_WINDOW_VALUES and (best_total is None or sum(window) > best_total[0]):
            best_total = (sum(window), end)
    hour_peak = _peak(times, precip, now_h - timedelta(hours=3), now_h + timedelta(hours=24))
    sev_total = common.severity_for(best_total[0] if best_total else None, th["rain_24h_mm"])
    sev_hour = common.severity_for(hour_peak[0] if hour_peak else None, th["rain_1h_mm"])
    sev = common.worst(sev_total, sev_hour)
    if sev is not None:
        # when is the worst of it: the 1 h peak if that drove the level, else the wettest hour of the 24 h window
        if sev_hour == sev and hour_peak:
            peak_at = hour_peak[1]
        else:
            wend = best_total[1]
            inside = _peak(times, precip, wend - timedelta(hours=23), wend)
            peak_at = inside[1] if inside else wend
        lead = _hours_between(peak_at, now)
        total = best_total[0] if best_total else None
        bits = []
        if total is not None:
            bits.append(f"{total:.0f} mm over a 24 h window ending {best_total[1]:%d %b %H:%M} UTC")
        if hour_peak:
            bits.append(f"peak {hour_peak[0]:.1f} mm/h around {hour_peak[1]:%d %b %H:%M} UTC")
        out.append(_make(
            city, "rain", endpoint="forecast", title=f"{RAIN_LABEL[sev]} for {city.label}: " + "; ".join(bits[:1] or bits),
            text="; ".join(bits) + ". Model forecast (Open-Meteo), not a station observation.", event=RAIN_LABEL[sev],
            hazards=["rain"], severity=sev, urgency=common.urgency_for_lead(lead), certainty=_certainty(lead),
            effective_at=peak_at, now=now, fetched_at=fetched_at,
            metadata={"total_24h_mm": round(total, 1) if total is not None else None,
                      "peak_1h_mm": round(hour_peak[0], 1) if hour_peak else None, "peak_at": peak_at.isoformat()}))

    # -- wind gusts ---------------------------------------------------------------------------------------
    gust = _peak(times, _values(block, "wind_gusts_10m", n), now_h - timedelta(hours=3), now_h + timedelta(hours=24))
    sev = common.severity_for(gust[0] if gust else None, th["wind_gust_kmh"])
    if sev is not None:
        lead = _hours_between(gust[1], now)
        out.append(_make(
            city, "wind", endpoint="forecast", title=f"Strong winds for {city.label}: gusts to {gust[0]:.0f} km/h",
            text=f"Peak gust {gust[0]:.0f} km/h around {gust[1]:%d %b %H:%M} UTC. Model forecast (Open-Meteo).",
            event="Strong Winds", hazards=["wind"], severity=sev, urgency=common.urgency_for_lead(lead),
            certainty=_certainty(lead), effective_at=gust[1], now=now, fetched_at=fetched_at,
            metadata={"peak_gust_kmh": round(gust[0], 1), "peak_at": gust[1].isoformat()}))

    # -- heat -------------------------------------------------------------------------------------------
    temp = _peak(times, _values(block, "temperature_2m", n), now_h, now_h + timedelta(hours=48))
    sev = common.severity_for(temp[0] if temp else None, th["heat_c"])
    if sev is not None:
        lead = _hours_between(temp[1], now)
        out.append(_make(
            city, "heat", endpoint="forecast", title=f"Extreme heat for {city.label}: up to {temp[0]:.0f} °C",
            text=f"Peak temperature {temp[0]:.0f} °C around {temp[1]:%d %b %H:%M} UTC. Model forecast (Open-Meteo).",
            event="Heat", hazards=["heat"], severity=sev, urgency=common.urgency_for_lead(lead),
            certainty=_certainty(lead), effective_at=temp[1], now=now, fetched_at=fetched_at,
            metadata={"peak_temp_c": round(temp[0], 1), "peak_at": temp[1].isoformat()}))

    # -- thunderstorm (WMO weather codes 95 / 96 / 99) ---------------------------------------------------
    codes = _values(block, "weather_code", n)
    storm: Optional[Tuple[SignalSeverity, datetime, int]] = None
    for t, c in zip(times, codes):
        if t is None or c is None or not (now_h - timedelta(hours=1) <= t <= now_h + timedelta(hours=12)):
            continue
        level = THUNDER_CODES.get(int(round(c)))
        if level and (storm is None or common.SEVERITY_RANK[level] > common.SEVERITY_RANK[storm[0]]):
            storm = (level, t, int(round(c)))
    if storm is not None:
        lead = _hours_between(storm[1], now)
        label = "Thunderstorm with hail" if storm[2] in (96, 99) else "Thunderstorm"
        out.append(_make(
            city, "storm", endpoint="forecast", title=f"{label} expected over {city.label}",
            text=f"{label} (WMO code {storm[2]}) around {storm[1]:%d %b %H:%M} UTC. Model forecast (Open-Meteo).",
            event=label, hazards=["lightning"], severity=storm[0], urgency=common.urgency_for_lead(lead),
            certainty=_certainty(lead), effective_at=storm[1], now=now, fetched_at=fetched_at,
            metadata={"weather_code": storm[2], "peak_at": storm[1].isoformat()}))
    return out


def analyze_flood(data: Any, city: CityRef, now: datetime, fetched_at: Optional[datetime] = None) -> List[Signal]:
    """Flood-API document -> at most one river-flood signal: the forecast peak river discharge compared with the
    median of the preceding days. No baseline, tiny rivers and missing values give no signal (never a guess)."""
    fetched_at = fetched_at or now
    block = _block(data, "daily")
    days = _times(block)
    if not any(days):
        raise OpenMeteoParseError("daily dates unreadable")
    n = len(days)
    ctrl = _values(block, "river_discharge", n)
    ens_median = _values(block, "river_discharge_median", n)
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    past = [v for d, v in zip(days, ctrl) if d is not None and v is not None and d < today and v >= 0]
    if len(past) < MIN_BASELINE_DAYS:
        return []
    baseline = statistics.median(past)
    if baseline <= 0:
        return []
    horizon = today + timedelta(days=flood_horizon_days())
    peak = _peak(days, ctrl, today, horizon)
    if peak is None or peak[0] < flood_min_discharge():
        return []
    ratios = thresholds()["flood_ratios"]
    ratio = peak[0] / baseline
    sev = common.severity_for(ratio, ratios)
    if sev is None:
        return []
    # "likely" only when the ensemble median forecast agrees; the control run alone is "possible"
    med_peak = _peak(days, ens_median, today, horizon)
    agrees = med_peak is not None and med_peak[0] / baseline >= ratios["minor"]
    lead = _hours_between(peak[1], now)
    return [_make(
        city, "flood", endpoint="flood", title=f"River discharge near {city.label} forecast at {ratio:.1f}x its recent level",
        text=(f"Forecast peak river discharge {peak[0]:.0f} m3/s on {peak[1]:%d %b} versus a recent median of "
              f"{baseline:.0f} m3/s. GloFAS model on a ~5 km grid: the nearest river cell, not a gauge."),
        event="River Discharge Surge", hazards=["flood"], severity=sev, urgency=common.urgency_for_lead(lead),
        certainty="likely" if agrees and lead <= certain_within_hours() + 24 else "possible",
        effective_at=max(peak[1], today), now=now, fetched_at=fetched_at,
        metadata={"peak_discharge_m3s": round(peak[0], 1), "baseline_discharge_m3s": round(baseline, 1),
                  "ratio": round(ratio, 2), "peak_date": peak[1].date().isoformat(), "ensemble_median_agrees": agrees})]


# -- network -----------------------------------------------------------------------------------------------
async def fetch_open_meteo(cities: List[CityRef], client: Optional[httpx.AsyncClient] = None,
                           now: Optional[datetime] = None) -> OpenMeteoResult:
    """Query both APIs for every city and turn what crosses a threshold into signals. Never raises."""
    s = get_settings()
    result = OpenMeteoResult(cities=len(cities))
    now = now or datetime.utcnow()
    own = client is None
    client = client or httpx.AsyncClient(
        timeout=s.open_meteo_timeout_seconds, headers={"User-Agent": "disaster-relief-coordinator/1.0 (decision support)"})
    try:
        for city in cities:
            jobs = [("forecast", s.open_meteo_forecast_url, {
                "latitude": round(city.lat, 4), "longitude": round(city.lng, 4), "timezone": "UTC",
                "hourly": "precipitation,wind_gusts_10m,temperature_2m,weather_code", "past_days": 1, "forecast_days": 3,
            }, analyze_forecast)]
            if s.open_meteo_flood_enabled:
                jobs.append(("flood", s.open_meteo_flood_url, {
                    "latitude": round(city.lat, 4), "longitude": round(city.lng, 4),
                    "daily": "river_discharge,river_discharge_median", "past_days": flood_past_days(),
                    "forecast_days": flood_horizon_days() + 1,
                }, analyze_flood))
            for endpoint, url, params, analyze in jobs:
                try:
                    data = await common.get_json_bounded(client, url, params, s.open_meteo_max_bytes)
                except FetchError as exc:
                    result.requests_failed += 1
                    result.err(f"{city.slug}/{endpoint}: {exc}")
                    log.warning("Open-Meteo %s for %s failed: %s", endpoint, city.slug, exc)
                    continue
                try:
                    result.signals.extend(analyze(data, city, now))
                except OpenMeteoParseError as exc:
                    result.malformed += 1
                    result.err(f"{city.slug}/{endpoint}: {exc}")
                    continue
                except Exception as exc:  # an unexpected shape must cost one document, not the refresh
                    result.malformed += 1
                    result.err(f"{city.slug}/{endpoint}: unexpected {type(exc).__name__}: {exc}")
                    log.exception("Open-Meteo %s for %s: analysis crashed", endpoint, city.slug)
                    continue
                result.requests_ok += 1
                result.scopes_ok.add((city.slug, endpoint))
    except Exception as exc:  # belt and braces: the contract is "never raises"
        result.err(f"unexpected: {type(exc).__name__}: {exc}")
        log.exception("Open-Meteo fetch crashed")
    finally:
        if own:
            await client.aclose()
    return result
