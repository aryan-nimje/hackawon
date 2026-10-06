"""Scoring constants and formulas for external evidence. Every number is overridable from the environment.

    weight    = trust(source) x ( w_sev*severity + w_cert*certainty + w_urg*urgency )      [0..1], fixed per signal
    freshness = 0.5 ** (age_hours / half_life), never below `floor` while the signal is in force
    score     = weight x freshness

Overrides (see .env.example) are validated; a malformed value is logged and the default is used, so a typo in
the environment can never take the backend down.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Dict, Optional

from config import get_settings
from services.signals.models import Signal, SignalSource, SignalStatus

log = logging.getLogger("signals.scoring")

# Source trust is the CEILING of a signal's weight (weight = trust x mix, mix <= 1). Official alerts are trusted most;
# Open-Meteo is model output (no issuing authority), so it tops out at 0.7; news is unverified text and tops out at 0.5.
DEFAULT_SOURCE_TRUST: Dict[str, float] = {"sachet": 0.9, "gdelt": 0.5, "open_meteo": 0.7}
_TRUST_SETTING = {  # source -> Settings attribute holding its optional override (env SIGNAL_TRUST_<SOURCE>)
    SignalSource.SACHET: "signal_trust_sachet",
    SignalSource.GDELT: "signal_trust_gdelt",
    SignalSource.OPEN_METEO: "signal_trust_open_meteo",
}
DEFAULT_SEVERITY_WEIGHTS: Dict[str, float] = {"extreme": 1.0, "severe": 0.8, "moderate": 0.5, "minor": 0.25, "unknown": 0.3}
DEFAULT_CERTAINTY_WEIGHTS: Dict[str, float] = {
    "observed": 1.0, "likely": 0.8, "possible": 0.5, "unlikely": 0.2, "unknown": 0.4,
}
DEFAULT_URGENCY_WEIGHTS: Dict[str, float] = {
    "immediate": 1.0, "expected": 0.7, "future": 0.4, "past": 0.1, "unknown": 0.4,
}
DEFAULT_COMPONENT_WEIGHTS: Dict[str, float] = {"severity": 0.5, "certainty": 0.3, "urgency": 0.2}
DEFAULT_FRESHNESS_HALF_LIFE_HOURS = 12.0
DEFAULT_FRESHNESS_FLOOR = 0.1


def _clamp01(x: float) -> float:
    return max(0.0, min(1.0, x))


def _json_table(name: str, raw: Optional[str], default: Dict[str, float]) -> Dict[str, float]:
    """Defaults, overlaid with the valid entries (numbers 0..1) of a JSON object from the environment."""
    table = dict(default)
    if not raw:
        return table
    try:
        parsed = json.loads(raw)
        if not isinstance(parsed, dict):
            raise ValueError("expected a JSON object")
    except (ValueError, TypeError) as exc:
        log.warning("%s ignored (%s); using defaults", name, exc)
        return table
    for key, val in parsed.items():
        if isinstance(val, (int, float)) and not isinstance(val, bool) and 0.0 <= float(val) <= 1.0:
            table[str(key).lower()] = float(val)
        else:
            log.warning("%s: ignoring %r=%r (need a number between 0 and 1)", name, key, val)
    return table


def source_trust(source: SignalSource) -> float:
    s = get_settings()
    trust = DEFAULT_SOURCE_TRUST.get(source.value, 0.5)
    attr = _TRUST_SETTING.get(source)
    override = getattr(s, attr, None) if attr else None
    if override is not None:
        if 0.0 <= override <= 1.0:
            trust = override
        else:
            log.warning("%s=%r ignored (need 0..1)", attr.upper(), override)
    return trust


def severity_weights() -> Dict[str, float]:
    return _json_table("SIGNAL_SEVERITY_WEIGHTS", get_settings().signal_severity_weights, DEFAULT_SEVERITY_WEIGHTS)


def certainty_weights() -> Dict[str, float]:
    return _json_table("SIGNAL_CERTAINTY_WEIGHTS", get_settings().signal_certainty_weights, DEFAULT_CERTAINTY_WEIGHTS)


def urgency_weights() -> Dict[str, float]:
    return _json_table("SIGNAL_URGENCY_WEIGHTS", get_settings().signal_urgency_weights, DEFAULT_URGENCY_WEIGHTS)


def component_weights() -> Dict[str, float]:
    """Relative importance of severity / certainty / urgency. Normalized to sum to 1 (all zero -> defaults)."""
    w = _json_table("SIGNAL_COMPONENT_WEIGHTS", get_settings().signal_component_weights, DEFAULT_COMPONENT_WEIGHTS)
    w = {k: w[k] for k in DEFAULT_COMPONENT_WEIGHTS}
    total = sum(w.values())
    return {k: v / total for k, v in w.items()} if total > 0 else dict(DEFAULT_COMPONENT_WEIGHTS)


def freshness_half_life_hours() -> float:
    v = get_settings().signal_freshness_half_life_hours
    if v is None:
        return DEFAULT_FRESHNESS_HALF_LIFE_HOURS
    if v <= 0:
        log.warning("SIGNAL_FRESHNESS_HALF_LIFE_HOURS=%r ignored (need > 0)", v)
        return DEFAULT_FRESHNESS_HALF_LIFE_HOURS
    return v


def freshness_floor() -> float:
    v = get_settings().signal_freshness_floor
    if v is None:
        return DEFAULT_FRESHNESS_FLOOR
    if not 0.0 <= v <= 1.0:
        log.warning("SIGNAL_FRESHNESS_FLOOR=%r ignored (need 0..1)", v)
        return DEFAULT_FRESHNESS_FLOOR
    return v


def signal_weight(signal: Signal) -> float:
    """trust x weighted mix of severity / certainty / urgency, 0..1. Independent of time."""
    w = component_weights()
    mix = (
        w["severity"] * severity_weights().get(signal.severity.value, severity_weights()["unknown"])
        + w["certainty"] * certainty_weights().get(signal.certainty, certainty_weights()["unknown"])
        + w["urgency"] * urgency_weights().get(signal.urgency, urgency_weights()["unknown"])
    )
    return _clamp01(source_trust(signal.source) * mix)


def is_in_force(signal: Signal, now: Optional[datetime] = None) -> bool:
    """Active status and not expired. (A warning that starts later is still in force: forecasts are useful early.)"""
    now = now or datetime.utcnow()
    if signal.status != SignalStatus.ACTIVE:
        return False
    if signal.expires_at is not None and signal.expires_at <= now:
        return False
    return True


def freshness(signal: Signal, now: Optional[datetime] = None) -> float:
    """1.0 when just issued, halving every half-life, never below the floor. 0.0 once out of force."""
    now = now or datetime.utcnow()
    if not is_in_force(signal, now):
        return 0.0
    start = signal.issued_at or signal.effective_at or signal.fetched_at
    age_h = max(0.0, (now - start).total_seconds() / 3600.0)
    return max(freshness_floor(), 0.5 ** (age_h / freshness_half_life_hours()))


def evidence_score(signal: Signal, now: Optional[datetime] = None) -> float:
    """Current strength of this signal as evidence, 0..1."""
    return _clamp01(signal.weight * freshness(signal, now))
