"""Helpers shared by the Open-Meteo and GDELT sources (SACHET keeps its own).

Everything from the network is untrusted: downloads are size-capped and never follow redirects, JSON that is not what
we expect is an error for that one request (never for the process), and a bad config value falls back to its default.
"""

from __future__ import annotations

import json
import logging
import math
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import httpx

from services import city as city_service
from services.osm import slugify
from services.signals.models import SignalSeverity

log = logging.getLogger("signals.common")

MAX_ERRORS_KEPT = 10
_CTRL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")

# Ascending severity: used for ladders and for "take the worst of".
LADDER_KEYS: Tuple[str, ...] = ("minor", "moderate", "severe", "extreme")
SEVERITY_RANK = {SignalSeverity.UNKNOWN: 0, SignalSeverity.MINOR: 1, SignalSeverity.MODERATE: 2,
                 SignalSeverity.SEVERE: 3, SignalSeverity.EXTREME: 4}


def clean_text(text: Any, limit: int) -> str:
    """Whitespace-collapsed, control-character-free, length-bounded. Non-strings become ''."""
    if not isinstance(text, str):
        return ""
    return _CTRL.sub("", " ".join(text.split()))[:limit]


def as_float(value: Any) -> Optional[float]:
    """A finite number from a JSON value, else None (None, bool, NaN, inf, text that is not a number)."""
    if isinstance(value, bool) or value is None:
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def parse_naive_utc(value: Any) -> Optional[datetime]:
    """ISO-8601 text -> naive UTC datetime; None when it cannot be read or is absurd."""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        dt = datetime.fromisoformat(value.strip().replace("Z", "+00:00").replace("z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt if 1990 <= dt.year <= 2100 else None


# -- bounded JSON download ---------------------------------------------------------------------------------
class FetchError(Exception):
    """One request failed (network, status, size, not JSON). The message is short and safe to show.

    `status` is the HTTP status when the server answered, `retry_after` the seconds it asked us to wait (if it said)."""

    def __init__(self, message: str = "", status: Optional[int] = None, retry_after: Optional[float] = None) -> None:
        super().__init__(message)
        self.status = status
        self.retry_after = retry_after


async def get_json_bounded(client: httpx.AsyncClient, url: str, params: Dict[str, Any], max_bytes: int) -> Any:
    """GET with a hard size cap and no redirects; returns the parsed JSON. Raises FetchError on any failure."""
    try:
        async with client.stream("GET", url, params=params, follow_redirects=False) as resp:
            body = bytearray()
            if int(resp.headers.get("content-length") or 0) > max_bytes:
                raise FetchError("response too large")
            async for chunk in resp.aiter_bytes():
                body += chunk
                if len(body) > max_bytes:
                    raise FetchError("response too large")
            status = resp.status_code
            retry_header = resp.headers.get("retry-after")
    except FetchError:
        raise
    except (httpx.HTTPError, ValueError) as exc:
        raise FetchError(f"{type(exc).__name__}: {exc}"[:200]) from exc
    if status != 200:
        reason = ""
        try:  # Open-Meteo explains 400s as {"error": true, "reason": "..."}
            parsed = json.loads(bytes(body))
            if isinstance(parsed, dict):
                reason = clean_text(parsed.get("reason"), 120)
        except ValueError:
            pass
        retry_after: Optional[float] = None
        try:  # seconds form only; an HTTP-date is rare here and simply ignored
            retry_after = max(0.0, float(retry_header)) if retry_header else None
        except ValueError:
            retry_after = None
        raise FetchError(f"HTTP {status}" + (f": {reason}" if reason else ""), status=status, retry_after=retry_after)
    if not body.strip():
        raise FetchError("empty response")
    try:
        return json.loads(bytes(body))
    except (ValueError, RecursionError):
        preview = clean_text(bytes(body[:160]).decode("utf-8", "replace"), 120)  # GDELT answers errors in plain text
        raise FetchError(f"not JSON: {preview}") from None


# -- cities (where a signal is placed) ----------------------------------------------------------------------
@dataclass(frozen=True)
class CityRef:
    slug: str
    label: str  # short name, e.g. "Pune"
    lat: float
    lng: float
    bbox: List[float]  # [south, west, north, east]


def city_ref(slug: str) -> Optional[CityRef]:
    """A cached city as a CityRef, or None when it is not cached / its file is unusable."""
    try:
        layers = city_service.load_cached(slug)
    except Exception:
        log.warning("city layers for %r unreadable", slug, exc_info=True)
        return None
    info = (layers or {}).get("city") or {}
    box = info.get("bbox")
    if not (isinstance(box, list) and len(box) == 4 and all(as_float(v) is not None for v in box)):
        return None
    box = [float(v) for v in box]
    centre = info.get("center")
    if isinstance(centre, list) and len(centre) == 2 and all(as_float(v) is not None for v in centre):
        lat, lng = float(centre[0]), float(centre[1])
    else:
        lat, lng = (box[0] + box[2]) / 2, (box[1] + box[3]) / 2
    name = str(info.get("name") or slug)
    label = re.sub(r"[\"'()]", "", name.split(",")[0].split("(")[0]).strip() or slug
    return CityRef(slug=slug, label=label, lat=lat, lng=lng, bbox=box)


def resolve_cities(names: List[str]) -> Tuple[List[CityRef], List[str]]:
    """Names / slugs -> (cities that can be placed, one error line per name that cannot)."""
    out: List[CityRef] = []
    errors: List[str] = []
    seen = set()
    for name in names:
        slug = slugify(name)
        if not slug or slug in seen:
            continue
        seen.add(slug)
        ref = city_ref(slug)
        if ref is None:
            errors.append(f"city {name!r}: no cached layers, so it cannot be placed")
        else:
            out.append(ref)
    return out, errors


# -- threshold ladders (configurable) -----------------------------------------------------------------------
def parse_ladder(name: str, raw: Optional[str], default: Dict[str, float]) -> Dict[str, float]:
    """Defaults overlaid with a JSON object of minor / moderate / severe / extreme thresholds from the environment.
    Anything unusable (not JSON, not an object, unknown key, negative / non-numeric value, thresholds that do not
    rise with severity) is logged and the defaults are used."""
    if not raw:
        return dict(default)
    try:
        parsed = json.loads(raw)
        if not isinstance(parsed, dict):
            raise ValueError("expected a JSON object")
        merged = dict(default)
        for key, val in parsed.items():
            k = str(key).lower()
            f = as_float(val)
            if k not in LADDER_KEYS:
                raise ValueError(f"unknown level {key!r} (use {', '.join(LADDER_KEYS)})")
            if f is None or f < 0:
                raise ValueError(f"{key!r} needs a number >= 0")
            merged[k] = f
        values = [merged[k] for k in LADDER_KEYS]
        if any(b < a for a, b in zip(values, values[1:])):
            raise ValueError("thresholds must not decrease from minor to extreme")
    except ValueError as exc:
        log.warning("%s ignored (%s); using defaults", name, exc)
        return dict(default)
    return merged


def severity_for(value: Optional[float], ladder: Dict[str, float]) -> Optional[SignalSeverity]:
    """The highest level whose threshold `value` reaches, or None when it is below `minor` (or missing)."""
    if value is None:
        return None
    hit: Optional[SignalSeverity] = None
    for key in LADDER_KEYS:
        if value >= ladder[key]:
            hit = SignalSeverity(key)
    return hit


def worst(*levels: Optional[SignalSeverity]) -> Optional[SignalSeverity]:
    found = [l for l in levels if l is not None]
    return max(found, key=lambda l: SEVERITY_RANK[l]) if found else None


def positive_or(value: Optional[float], default: float, name: str) -> float:
    """A configured number that must be > 0; otherwise the default (logged)."""
    f = as_float(value)
    if f is None or f <= 0:
        log.warning("%s=%r ignored (need > 0); using %s", name, value, default)
        return default
    return f


def urgency_for_lead(lead_hours: float) -> str:
    """CAP-style urgency from how far ahead the peak is (negative = already happening / past)."""
    if lead_hours < -6:
        return "past"
    if lead_hours <= 3:
        return "immediate"
    if lead_hours <= 24:
        return "expected"
    return "future"
