"""Credibility corroboration: external evidence (signals) and crowd agreement. Rule-based, no reporter identity.

Two independent boosts are added to an incident's credibility by `agents/verification.py`:

  * EVIDENCE: a signal from the common signals store (SACHET alert, GDELT news, Open-Meteo weather, later river
    gauges) that is hazard-compatible with the incident, covers its location, and overlaps it in time. Broad,
    district- or city-wide evidence gives the smaller boost; local evidence (a small alert area, a river gauge, news
    that names the incident's locality) gives the larger one. No matching signal = no boost and NO penalty.
  * CROWD: other citizen reports about the same event nearby. Only INDEPENDENT reports count: copy-pasted text, and
    near-identical reports sent from the same spot or at the same moment, are counted once. Independence uses text
    diversity, spatial spread and time spread only. There are no reporter ids, no history and no tracking.

Every number lives in `CorroborationConfig` below (the one configurable block). Override any of them with the single
environment variable CORROBORATION_CONFIG, a JSON object, e.g. {"evidence_boost_local": 0.15}. Invalid entries are
logged and ignored (defaults stay), so a typo can never take the backend down. The credibility ceiling can be lowered
but never raised above 0.95.
"""

from __future__ import annotations

import json
import logging
import math
import re
from dataclasses import asdict, dataclass, fields
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from functools import lru_cache
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

from config import get_settings
from services.city import city_label
from services.signals import geo
from services.signals.models import Signal, SignalKind, SignalSource, SignalStatus
from services.signals.sachet import hazards_for
from state import Incident, IncidentSource

log = logging.getLogger("corroboration")

HARD_MAX_CREDIBILITY = 0.95  # no configuration can raise the ceiling above this
MAX_CROWD_SCAN = 500  # safety bound on how many nearby reports are compared per incident (not a scoring number)


# =====================================================================================================
# THE configurable block: every scoring number added for corroboration is here and nowhere else.
# =====================================================================================================
@dataclass(frozen=True)
class CorroborationConfig:
    # -- ceiling ---------------------------------------------------------------------------------------
    max_credibility: float = 0.95  # final credibility never exceeds this (and never exceeds 0.95)

    # -- external evidence (signals) -------------------------------------------------------------------
    evidence_boost_broad: float = 0.05  # district- / city-wide evidence
    evidence_boost_local: float = 0.12  # local evidence: small alert area, river gauge, news naming the locality
    evidence_extra_source_bonus: float = 0.03  # added per additional DIFFERENT source that also supports
    evidence_max_boost: float = 0.20  # ceiling for the whole evidence boost
    evidence_min_signal_weight: float = 0.20  # signals weaker than this (trust x severity/certainty/urgency) are ignored
    evidence_time_before_hours: float = 6.0  # an incident up to this long BEFORE a signal began can still be supported
    evidence_time_after_hours: float = 3.0  # ... and up to this long AFTER it expired
    evidence_default_ttl_hours: float = 24.0  # signal lifetime when it carries no expiry
    evidence_local_radius_km: float = 3.0  # point evidence (river gauge) supports incidents within this distance
    evidence_local_area_max_km: float = 10.0  # an alert area whose box diagonal is at most this counts as local
    evidence_point_radius_km: float = 25.0  # a signal with a point but no area is treated as this wide

    # -- contradicting evidence (signals marked metadata.stance == "contradicts": all clear / false alarm, normal conditions)
    # Matched exactly like supporting evidence (hazard, place, time, minimum weight), then SUBTRACTED from credibility.
    contradiction_penalty_broad: float = 0.10  # district- / city-wide contradiction
    contradiction_penalty_local: float = 0.25  # local contradiction
    contradiction_extra_source_penalty: float = 0.05  # added per additional DIFFERENT source that also contradicts
    contradiction_max_penalty: float = 0.40  # ceiling for the whole contradiction penalty

    # -- crowd corroboration ---------------------------------------------------------------------------
    crowd_boost_tiers: Tuple[Tuple[int, float], ...] = ((2, 0.10), (3, 0.15), (5, 0.20), (10, 0.25), (25, 0.35))
    crowd_cap: int = 25  # independent reports counted for the boost never exceed this
    crowd_radius_km: float = 1.0  # reports within this distance describe the same event
    crowd_window_hours: float = 6.0  # ... and within this time of each other
    crowd_copy_similarity: float = 0.85  # text this similar (0..1) is a copy-paste: never independent
    crowd_similar_text: float = 0.60  # text this similar AND sent from the same spot or moment: not independent
    crowd_same_spot_m: float = 25.0  # "same spot"
    crowd_same_moment_s: float = 120.0  # "same moment"


# (kind, low, high) per scalar field; "int" values are whole numbers. Anything outside the range is ignored.
_RULES: Dict[str, Tuple[str, float, float]] = {
    "max_credibility": ("float", 0.0, 1.0),
    "evidence_boost_broad": ("float", 0.0, 1.0),
    "evidence_boost_local": ("float", 0.0, 1.0),
    "evidence_extra_source_bonus": ("float", 0.0, 1.0),
    "evidence_max_boost": ("float", 0.0, 1.0),
    "evidence_min_signal_weight": ("float", 0.0, 1.0),
    "evidence_time_before_hours": ("float", 0.0, 720.0),
    "evidence_time_after_hours": ("float", 0.0, 720.0),
    "evidence_default_ttl_hours": ("float", 0.1, 720.0),
    "evidence_local_radius_km": ("float", 0.01, 500.0),
    "evidence_local_area_max_km": ("float", 0.01, 500.0),
    "evidence_point_radius_km": ("float", 0.01, 500.0),
    "contradiction_penalty_broad": ("float", 0.0, 1.0),
    "contradiction_penalty_local": ("float", 0.0, 1.0),
    "contradiction_extra_source_penalty": ("float", 0.0, 1.0),
    "contradiction_max_penalty": ("float", 0.0, 1.0),
    "crowd_cap": ("int", 2, 10000),
    "crowd_radius_km": ("float", 0.001, 500.0),
    "crowd_window_hours": ("float", 0.01, 720.0),
    "crowd_copy_similarity": ("float", 0.0, 1.0),
    "crowd_similar_text": ("float", 0.0, 1.0),
    "crowd_same_spot_m": ("float", 0.0, 100000.0),
    "crowd_same_moment_s": ("float", 0.0, 604800.0),
}


def _valid_tiers(value: Any) -> Optional[Tuple[Tuple[int, float], ...]]:
    """[[count, boost], ...] with counts >= 2 strictly rising and boosts in 0..1 never falling; else None."""
    if not isinstance(value, list) or not 1 <= len(value) <= 12:
        return None
    out: List[Tuple[int, float]] = []
    for row in value:
        if not (isinstance(row, (list, tuple)) and len(row) == 2):
            return None
        n, b = row
        if isinstance(n, bool) or isinstance(b, bool) or not isinstance(n, int) or not isinstance(b, (int, float)):
            return None
        if n < 2 or not (0.0 <= float(b) <= 1.0) or not math.isfinite(float(b)):
            return None
        if out and (n <= out[-1][0] or float(b) < out[-1][1]):
            return None
        out.append((n, float(b)))
    return tuple(out)


@lru_cache(maxsize=16)
def _parse_overrides(raw: Optional[str]) -> CorroborationConfig:
    base = CorroborationConfig()
    if not raw:
        return base
    try:
        parsed = json.loads(raw)
        if not isinstance(parsed, dict):
            raise ValueError("expected a JSON object")
    except (ValueError, TypeError) as exc:
        log.warning("CORROBORATION_CONFIG ignored (%s); using defaults", exc)
        return base
    changes: Dict[str, Any] = {}
    known = {f.name for f in fields(CorroborationConfig)}
    for key, val in parsed.items():
        if key not in known:
            log.warning("CORROBORATION_CONFIG: unknown key %r ignored", key)
        elif key == "crowd_boost_tiers":
            tiers = _valid_tiers(val)
            if tiers is None:
                log.warning("CORROBORATION_CONFIG: crowd_boost_tiers ignored (need [[count>=2, boost 0..1], ...] rising)")
            else:
                changes[key] = tiers
        else:
            kind, lo, hi = _RULES[key]
            ok = isinstance(val, (int, float)) and not isinstance(val, bool) and math.isfinite(float(val))
            ok = ok and lo <= float(val) <= hi and (kind == "float" or float(val) == int(val))
            if key == "max_credibility" and isinstance(val, (int, float)) and not isinstance(val, bool) and val > HARD_MAX_CREDIBILITY:
                log.warning("CORROBORATION_CONFIG: max_credibility %r lowered to %s", val, HARD_MAX_CREDIBILITY)
                changes[key] = HARD_MAX_CREDIBILITY
            elif ok:
                changes[key] = int(val) if kind == "int" else float(val)
            else:
                log.warning("CORROBORATION_CONFIG: %s=%r ignored (need %s between %s and %s)", key, val, kind, lo, hi)
    return CorroborationConfig(**{**asdict(base), **changes})


def config() -> CorroborationConfig:
    """The block in force: defaults overlaid with valid entries of CORROBORATION_CONFIG."""
    return _parse_overrides(get_settings().corroboration_config)


def settings_in_force() -> Dict[str, Any]:
    d = asdict(config())
    d["crowd_boost_tiers"] = [list(t) for t in d["crowd_boost_tiers"]]
    return d


def credibility_ceiling(cfg: Optional[CorroborationConfig] = None) -> float:
    cfg = cfg or config()
    return min(cfg.max_credibility, HARD_MAX_CREDIBILITY)


# =====================================================================================================
# helpers
# =====================================================================================================
def _km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    a = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lng2 - lng1) / 2) ** 2
    return 2 * 6371.0 * math.asin(math.sqrt(a))


def _naive_utc(dt: datetime) -> datetime:
    return dt.astimezone(timezone.utc).replace(tzinfo=None) if dt.tzinfo is not None else dt


# =====================================================================================================
# hazard compatibility
# =====================================================================================================
# Hazard words an INCIDENT's own text may carry, beyond the shared tagger in sachet.py (which was built for alerts).
_INCIDENT_EXTRA = {
    "flood": re.compile(
        r"\b(?:water\s+(?:is\s+)?(?:rising|level|entering)|submerg|(?:knee|waist|chest|neck)[- ]deep|"
        r"swept\s+away|feet\s+deep|under\s?water)"
    ),
}

# Which signal hazards can support an incident about each hazard. Anything not listed here cannot: in particular a
# flood / rain alert never supports a fire incident, and a fire alert never supports a flood.
_SUPPORTED_BY: Dict[str, Set[str]] = {
    "flood": {"flood", "rain", "cyclone"},
    "rain": {"rain", "flood", "cyclone"},
    "cyclone": {"cyclone", "rain", "wind", "coastal", "flood"},
    "wind": {"wind", "cyclone"},
    "landslide": {"landslide", "rain", "flood"},
    "lightning": {"lightning", "rain"},
    "coastal": {"coastal", "cyclone"},
    "heat": {"heat"},
    "cold": {"cold"},
    "earthquake": {"earthquake"},
    "fire": {"fire"},
}
_EXCLUSIVE = ("fire", "earthquake")  # when the text is about one of these, no other hazard's text counts


def incident_hazards(incident: Incident) -> Set[str]:
    """Hazard tags the incident's text is about (empty when it names none: it then cannot be matched to a signal)."""
    text = incident.text or ""
    tags = set(hazards_for(text))
    low = text.lower()
    for tag, rx in _INCIDENT_EXTRA.items():
        if rx.search(low):
            tags.add(tag)
    for ex in _EXCLUSIVE:
        if ex in tags:
            return {ex}
    return tags


def supporting_hazards(incident_tags: Iterable[str]) -> Set[str]:
    out: Set[str] = set()
    for t in incident_tags:
        out |= _SUPPORTED_BY.get(t, {t})
    return out


def hazard_compatible(incident_tags: Set[str], signal_tags: Iterable[str]) -> bool:
    return bool(supporting_hazards(incident_tags) & set(signal_tags))


# =====================================================================================================
# external evidence
# =====================================================================================================
_LOCAL_TYPES = {"river_gauge", "gauge", "water_level"}
_SOURCE_LABEL = {
    SignalSource.SACHET: "SACHET official alert",
    SignalSource.GDELT: "news report",
    SignalSource.OPEN_METEO: "Open-Meteo weather model",
}


@dataclass
class Support:
    signal: Signal
    tier: str  # "local" | "broad"
    boost: float


def _signal_window(sig: Signal, cfg: CorroborationConfig) -> Tuple[datetime, datetime]:
    starts = [t for t in (sig.issued_at, sig.effective_at) if t is not None] or [sig.fetched_at]
    start = min(starts)
    end = sig.expires_at or start + timedelta(hours=cfg.evidence_default_ttl_hours)
    return start, end


def _time_overlaps(incident: Incident, sig: Signal, cfg: CorroborationConfig) -> bool:
    start, end = _signal_window(sig, cfg)
    ts = _naive_utc(incident.timestamp)
    return start - timedelta(hours=cfg.evidence_time_before_hours) <= ts <= end + timedelta(hours=cfg.evidence_time_after_hours)


def _inside_area(lat: float, lng: float, sig: Signal, cfg: CorroborationConfig) -> bool:
    if sig.polygons or sig.circles:
        if any(geo.point_in_ring(lat, lng, ring) for ring in sig.polygons if len(ring) >= 3):
            return True
        return any(_km(lat, lng, c[0], c[1]) <= c[2] for c in sig.circles if len(c) >= 3)
    if sig.bbox and len(sig.bbox) == 4:
        s, w, n, e = sig.bbox
        return s <= lat <= n and w <= lng <= e
    if sig.lat is not None and sig.lng is not None:
        return _km(lat, lng, sig.lat, sig.lng) <= cfg.evidence_point_radius_km
    return False


def _extent_km(sig: Signal) -> Optional[float]:
    if not (sig.bbox and len(sig.bbox) == 4):
        return None
    s, w, n, e = sig.bbox
    return _km(s, w, n, e)


def _locality_names(incident: Incident) -> List[str]:
    """Place names from the incident's location text that are specific enough to look for in a headline."""
    generic = {city_label().lower(), "unknown", "current location", "near me", "my location", "here", ""}
    names: List[str] = []
    for seg in (incident.location or "").split(",")[:2]:
        seg = re.sub(r"\s+", " ", re.sub(r"^\s*(?:near|at)\s+", "", seg.strip(), flags=re.I)).strip().lower()
        if len(seg) >= 4 and seg not in generic:
            names.append(seg)
    return names


def _names_locality(incident: Incident, sig: Signal) -> bool:
    blob = f"{sig.title} {sig.text}".lower()
    return any(re.search(r"\b" + re.escape(n) + r"\b", blob) for n in _locality_names(incident))


def _geo_tier(incident: Incident, sig: Signal, cfg: CorroborationConfig) -> Optional[str]:
    """'local', 'broad' or None (the signal's area does not cover the incident)."""
    meta = sig.metadata or {}
    if meta.get("scope") == "local" or meta.get("type") in _LOCAL_TYPES:  # point evidence, e.g. a river gauge
        if sig.lat is None or sig.lng is None:
            return None
        radius = max((c[2] for c in sig.circles if len(c) >= 3), default=cfg.evidence_local_radius_km)
        return "local" if _km(incident.lat, incident.lng, sig.lat, sig.lng) <= radius else None
    if not _inside_area(incident.lat, incident.lng, sig, cfg):
        return None
    if sig.kind == SignalKind.NEWS:  # news is placed at city level; naming the locality makes it local
        return "local" if _names_locality(incident, sig) else "broad"
    extent = _extent_km(sig)
    if extent is not None and extent <= cfg.evidence_local_area_max_km:
        return "local"
    return "broad"


CONTRADICTS = "contradicts"
TARGET_KEY = "target_incident_id"  # a signal addressed to ONE incident (only the Simulation app's evidence sets it)


def is_contradiction(sig: Signal) -> bool:
    """A signal that argues AGAINST the incident it matches (metadata.stance == "contradicts")."""
    return (sig.metadata or {}).get("stance") == CONTRADICTS


def _match(incident: Incident, signals: Iterable[Signal], cfg: CorroborationConfig, contradicting: bool) -> List[Support]:
    """Signals that match the incident (hazard, place, time, weight), strongest tier first. Never raises on odd signal data.
    `contradicting` picks the signals marked as contradictions; otherwise the supporting ones.

    A signal is matched on hazard, place and time. One that is explicitly addressed to this incident
    (metadata.target_incident_id) skips only the hazard-word test, because the incident's text may name no hazard;
    it still has to cover the place and the time. Real signals never carry that key, so real matching is unchanged."""
    tags = incident_hazards(incident)
    low, high = (
        (cfg.contradiction_penalty_broad, cfg.contradiction_penalty_local)
        if contradicting else (cfg.evidence_boost_broad, cfg.evidence_boost_local)
    )
    found: List[Support] = []
    for sig in signals:
        try:
            if is_contradiction(sig) != contradicting:
                continue
            if sig.status != SignalStatus.ACTIVE or sig.weight < cfg.evidence_min_signal_weight:
                continue
            targeted = (sig.metadata or {}).get(TARGET_KEY) == incident.id
            if not targeted and not (tags and hazard_compatible(tags, sig.hazards)):
                continue
            if not _time_overlaps(incident, sig, cfg):
                continue
            tier = _geo_tier(incident, sig, cfg)
        except Exception:
            log.warning("signal %s skipped while matching", getattr(sig, "id", "?"), exc_info=True)
            continue
        if tier:
            found.append(Support(sig, tier, high if tier == "local" else low))
    found.sort(key=lambda s: (-s.boost, -s.signal.weight, s.signal.id))
    return found


def find_support(incident: Incident, signals: Iterable[Signal], cfg: Optional[CorroborationConfig] = None) -> List[Support]:
    """Signals that support the incident, strongest tier first. Never raises on odd signal data."""
    return _match(incident, signals, cfg or config(), contradicting=False)


def find_contradiction(incident: Incident, signals: Iterable[Signal], cfg: Optional[CorroborationConfig] = None) -> List[Support]:
    """Signals that contradict the incident (`boost` holds the penalty for that signal's tier), strongest first."""
    return _match(incident, signals, cfg or config(), contradicting=True)


def evidence_boost(supports: List[Support], cfg: Optional[CorroborationConfig] = None) -> float:
    """Best single boost, plus a small bonus for each additional DIFFERENT source, capped."""
    if not supports:
        return 0.0
    cfg = cfg or config()
    sources = {s.signal.source for s in supports}
    return min(cfg.evidence_max_boost, supports[0].boost + cfg.evidence_extra_source_bonus * (len(sources) - 1))


def contradiction_penalty(contradictions: List[Support], cfg: Optional[CorroborationConfig] = None) -> float:
    """Largest single penalty, plus a small amount for each additional DIFFERENT source, capped."""
    if not contradictions:
        return 0.0
    cfg = cfg or config()
    sources = {s.signal.source for s in contradictions}
    return min(cfg.contradiction_max_penalty,
               contradictions[0].boost + cfg.contradiction_extra_source_penalty * (len(sources) - 1))


def _short(text: str, n: int = 110) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= n else text[: n - 1] + "…"


def support_reason(s: Support) -> str:
    """e.g. Supported by SACHET official alert "Heavy rain warning" (local area): https://... Never says 'verified'."""
    label = _SOURCE_LABEL.get(s.signal.source, "external report")
    area = "local evidence" if s.tier == "local" else "district/city-wide evidence"
    link = f": {s.signal.source_url}" if s.signal.source_url else ""
    return f"Supported by {label} \"{_short(s.signal.title)}\" ({area}){link}"


def contradiction_reason(s: Support) -> str:
    """e.g. Contradicted by SACHET official alert "All clear ..." (local evidence)."""
    label = _SOURCE_LABEL.get(s.signal.source, "external report")
    area = "local evidence" if s.tier == "local" else "district/city-wide evidence"
    link = f": {s.signal.source_url}" if s.signal.source_url else ""
    return f"Contradicted by {label} \"{_short(s.signal.title)}\" ({area}){link}"


# =====================================================================================================
# crowd corroboration
# =====================================================================================================
@dataclass
class Crowd:
    size: int = 1  # independent reports about this event, including this one (NOT capped: for severity / priority)
    nearby: int = 1  # reports found nearby before independence was applied
    boost: float = 0.0  # from min(size, crowd_cap)


def _is_plausible(inc: Incident) -> bool:
    return not (abs(inc.lat) < 1 and abs(inc.lng) < 1)


@lru_cache(maxsize=4096)
def _norm(text: str) -> Tuple[str, frozenset]:
    words = re.findall(r"[a-z0-9]+", (text or "").lower()[:400])
    return " ".join(words), frozenset(words)


def text_similarity(a: str, b: str) -> float:
    """0..1: the higher of word overlap and character-sequence similarity (catches copy-paste with small edits)."""
    na, wa = _norm(a)
    nb, wb = _norm(b)
    if not wa or not wb:
        return 0.0
    jaccard = len(wa & wb) / len(wa | wb)
    return max(jaccard, SequenceMatcher(None, na, nb).ratio())


def _dependent(a: Incident, b: Incident, cfg: CorroborationConfig) -> bool:
    """True when two reports should count as ONE: copy-paste, or near-identical text sent from the same spot or at
    the same moment, or distinct text but literally the same spot AND moment."""
    sim = text_similarity(a.text, b.text)
    if sim >= cfg.crowd_copy_similarity:
        return True
    same_spot = _km(a.lat, a.lng, b.lat, b.lng) * 1000.0 <= cfg.crowd_same_spot_m
    same_moment = abs((_naive_utc(a.timestamp) - _naive_utc(b.timestamp)).total_seconds()) <= cfg.crowd_same_moment_s
    if sim >= cfg.crowd_similar_text and (same_spot or same_moment):
        return True
    return same_spot and same_moment


def _countable(inc: Incident) -> bool:
    return inc.source == IncidentSource.CITIZEN and not (inc.raw_metadata or {}).get("suspicious") and _is_plausible(inc)


def crowd_boost_for(size: int, cfg: Optional[CorroborationConfig] = None) -> float:
    """Step boost for an independent-report count (counts above crowd_cap are treated as crowd_cap)."""
    cfg = cfg or config()
    n = min(size, cfg.crowd_cap)
    boost = 0.0
    for count, b in cfg.crowd_boost_tiers:
        if n >= count:
            boost = b
    return boost


def assess_crowd(incident: Incident, all_incidents: Iterable[Incident], cfg: Optional[CorroborationConfig] = None) -> Crowd:
    cfg = cfg or config()
    if not _countable(incident):
        return Crowd()
    tags = incident_hazards(incident)
    allowed = supporting_hazards(tags) if tags else set()
    ts = _naive_utc(incident.timestamp)
    window = timedelta(hours=cfg.crowd_window_hours)
    near: List[Tuple[float, Incident]] = []
    for other in all_incidents:
        if other.id == incident.id or not _countable(other):
            continue
        d = _km(incident.lat, incident.lng, other.lat, other.lng)
        if d > cfg.crowd_radius_km or abs(_naive_utc(other.timestamp) - ts) > window:
            continue
        other_tags = incident_hazards(other)
        if tags and other_tags and not (other_tags & allowed):  # clearly a different kind of event
            continue
        near.append((d, other))
    near.sort(key=lambda p: (p[0], p[1].id))
    members = [incident] + [o for _, o in near[:MAX_CROWD_SCAN]]
    members.sort(key=lambda m: (_naive_utc(m.timestamp), m.id))
    seen: List[Incident] = []
    independent = 0
    for m in members:
        if not any(_dependent(m, earlier, cfg) for earlier in seen):
            independent += 1
        seen.append(m)
    return Crowd(size=independent, nearby=len(members), boost=crowd_boost_for(independent, cfg))


def crowd_reason(c: Crowd, cfg: Optional[CorroborationConfig] = None) -> Optional[str]:
    if c.boost <= 0:
        return None
    cfg = cfg or config()
    counted = min(c.size, cfg.crowd_cap)
    text = f"Corroborated by {counted}{'+' if c.size > cfg.crowd_cap else ''} independent nearby reports (+{c.boost:.2f})"
    merged = c.nearby - c.size
    if merged > 0:
        text += f"; {merged} copy-pasted or same-spot report(s) counted once"
    return text
