"""Simulated evidence for the Simulation app's per-incident controls.

There is NO separate simulation scoring system. Each action here only *builds an ordinary `Signal`* (same model,
same `signal_store`, same hazard / place / time matching and the same weight formula as real SACHET, GDELT and
Open-Meteo signals). `agents/verification.py` then scores it like any other:

  * backs the incident  -> the existing supporting-evidence boost   (`corroboration.find_support`)
  * contradicts it      -> the contradiction penalty                (`corroboration.find_contradiction`)

What makes a signal simulated is only `metadata.simulated = True` (and the `sim:` id prefix). That is how
"Reset simulation" finds and removes exactly these signals and nothing real.

The signal is built from the incident itself: its hazard (from its text), its place (a ~2 km area around it) and
the current time.
"""

from __future__ import annotations

import math
import random
from datetime import datetime, timedelta, timezone
from typing import Dict, Iterable, Optional, Tuple

from services.corroboration import CONTRADICTS, TARGET_KEY, incident_hazards
from services.signals import scoring
from services.signals.models import Signal, SignalKind, SignalSeverity, SignalSource
from state import Incident

SUPPORTS = "supports"
SUPPORT_KINDS: Tuple[str, ...] = ("news", "official_alert", "weather")
CONTRADICT_KINDS: Tuple[str, ...] = ("all_clear", "normal_conditions")
SIM_ID_PREFIX = "sim:"

AREA_HALF_DEG = 0.01  # ~1.1 km each way: a small, local area around the incident
AREA_RADIUS_KM = 2.0
LIFETIME_HOURS = 6.0  # how long the simulated evidence stays in force

# Picks the one hazard to build evidence for when an incident's text names several.
_HAZARD_ORDER = ("flood", "rain", "cyclone", "landslide", "fire", "earthquake", "wind", "lightning", "coastal", "heat", "cold")

# hazard tag -> (phrase used in headlines, event name)
_PHRASE: Dict[str, Tuple[str, str]] = {
    "flood": ("flooding", "Flood"),
    "rain": ("heavy rain", "Heavy Rain"),
    "cyclone": ("cyclonic conditions", "Cyclone"),
    "landslide": ("landslide risk", "Landslide"),
    "fire": ("fire", "Fire"),
    "earthquake": ("seismic activity", "Earthquake"),
    "wind": ("strong winds", "High Wind"),
    "lightning": ("lightning and thunderstorms", "Thunderstorm"),
    "coastal": ("high waves and coastal surge", "Coastal Hazard"),
    "heat": ("extreme heat", "Heat Wave"),
    "cold": ("cold wave conditions", "Cold Wave"),
}

# One random source for the module; tests replace it to make the choice deterministic.
rng = random.Random()


class NoHazard(ValueError):
    """Neither the incident's text nor any other incident in its run names a hazard, so there is nothing to build evidence about."""


def is_simulated(sig: Signal) -> bool:
    return bool((sig.metadata or {}).get("simulated"))


def stance_of(kind: str) -> str:
    return SUPPORTS if kind in SUPPORT_KINDS else CONTRADICTS


def resolve_action(action: str, chooser: Optional[random.Random] = None) -> str:
    """The evidence kind for one request. `random` draws the stance and then the kind from `chooser` each time it is
    called, so every incident gets its own independent choice."""
    if action in SUPPORT_KINDS or action in CONTRADICT_KINDS:
        return action
    if action != "random":
        raise ValueError(f"unknown evidence action {action!r}")
    r = chooser or rng
    pool = SUPPORT_KINDS if r.random() < 0.5 else CONTRADICT_KINDS
    return r.choice(pool)


def _own_hazard(incident: Incident) -> Optional[str]:
    tags = incident_hazards(incident)
    for t in _HAZARD_ORDER:
        if t in tags:
            return t
    for t in sorted(tags):  # a tag outside the ordered list: still usable if it has a phrase
        if t in _PHRASE:
            return t
    return None


def _km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    a = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lng2 - lng1) / 2) ** 2
    return 2 * 6371.0 * math.asin(math.sqrt(a))


def primary_hazard(incident: Incident, context: Iterable[Incident] = ()) -> str:
    """The hazard to build evidence about: the one the incident's text names. Many reports name none ("needs insulin,
    trapped on 2nd floor"); they belong to the same event as the incidents around them, so they take the hazard of the
    NEAREST other incident in the run that names one."""
    own = _own_hazard(incident)
    if own:
        return own
    best: Optional[Tuple[float, str]] = None
    for other in context:
        if other.id == incident.id:
            continue
        tag = _own_hazard(other)
        if tag:
            d = _km(incident.lat, incident.lng, other.lat, other.lng)
            if best is None or d < best[0]:
                best = (d, tag)
    if best:
        return best[1]
    raise NoHazard("Neither this incident nor any other incident in the run names a hazard (flood, fire, rain, ...), "
                   "so there is nothing to build evidence about.")


def _naive_utc(dt: datetime) -> datetime:
    return dt.astimezone(timezone.utc).replace(tzinfo=None) if dt.tzinfo is not None else dt


def _place(incident: Incident) -> str:
    place = " ".join((incident.location or "").split()) or f"{incident.lat:.4f}, {incident.lng:.4f}"
    return place if len(place) <= 80 else place[:79] + "…"


def build_signal(incident: Incident, kind: str, now: Optional[datetime] = None, context: Iterable[Incident] = ()) -> Signal:
    """The simulated signal for `kind` about `incident` (`context`: the run's incidents, used when this one names no
    hazard). Raises NoHazard when no hazard can be found at all."""
    if kind not in SUPPORT_KINDS and kind not in CONTRADICT_KINDS:
        raise ValueError(f"unknown evidence kind {kind!r}")
    now = now or datetime.utcnow()
    tag = primary_hazard(incident, context)
    noun, event = _PHRASE[tag]
    place = _place(incident)

    # (source, kind, title, text, severity)
    specs = {
        "news": (SignalSource.GDELT, SignalKind.NEWS, f"Reports of {noun} near {place}",
                 f"News outlets report {noun} in the {place} area.", SignalSeverity.MODERATE),
        "official_alert": (SignalSource.SACHET, SignalKind.OFFICIAL_ALERT, f"{event} warning for {place}",
                           f"Official alert: {noun} in the {place} area. Follow instructions from local authorities.",
                           SignalSeverity.SEVERE),
        "weather": (SignalSource.OPEN_METEO, SignalKind.WEATHER, f"{event} conditions detected near {place}",
                    f"Weather model shows conditions consistent with {noun} near {place}.", SignalSeverity.MODERATE),
        "all_clear": (SignalSource.SACHET, SignalKind.OFFICIAL_ALERT, f"All clear / false alarm: no {noun} at {place}",
                      f"Official all clear: the earlier report of {noun} near {place} was a false alarm.",
                      SignalSeverity.MINOR),
        "normal_conditions": (SignalSource.OPEN_METEO, SignalKind.WEATHER,
                              f"Normal conditions near {place}: no {noun} indicators",
                              f"Weather model shows normal conditions near {place}; nothing consistent with {noun}.",
                              SignalSeverity.MINOR),
    }
    source, sig_kind, title, text, severity = specs[kind]
    stance = stance_of(kind)

    # The signal's lifetime must cover the incident's own time, which may be older than `now` (replayed scenarios).
    ts = _naive_utc(incident.timestamp)
    start = min(now, ts)
    sig = Signal(
        id=f"{SIM_ID_PREFIX}{incident.id}:{kind}",  # one per incident and kind: pressing it again refreshes it in place
        source=source,
        kind=sig_kind,
        title=title,
        text=text,
        event=event,
        hazards=[tag],
        severity=severity,
        urgency="immediate",
        certainty="observed" if source != SignalSource.GDELT else "likely",
        area=f"Around {place}",
        lat=incident.lat,
        lng=incident.lng,
        bbox=[incident.lat - AREA_HALF_DEG, incident.lng - AREA_HALF_DEG, incident.lat + AREA_HALF_DEG, incident.lng + AREA_HALF_DEG],
        circles=[[incident.lat, incident.lng, AREA_RADIUS_KM]],
        sender="Simulation",
        language="en",
        issued_at=now,
        effective_at=start,
        expires_at=max(now, ts) + timedelta(hours=LIFETIME_HOURS),
        fetched_at=now,
        # TARGET_KEY: addressed to this incident, whose own text may name no hazard (see corroboration._match)
        metadata={"simulated": True, "stance": stance, "evidence": kind, TARGET_KEY: incident.id},
    )
    # Same trust and weight formulas as every real signal.
    return sig.model_copy(update={"trust": scoring.source_trust(source), "weight": scoring.signal_weight(sig)})
