"""Unit tests for verification rules."""

from datetime import datetime

import pytest

from agents.verification import _score_incident, FLAG_THRESHOLD
from state import Incident, IncidentSource, NeedType, Urgency


def _incident(**kwargs) -> Incident:
    defaults = dict(
        id="test-1",
        text="Water rising, need rescue",
        location="Houston, TX",
        lat=29.76,
        lng=-95.37,
        need_type=NeedType.RESCUE,
        urgency=Urgency.HIGH,
        source=IncidentSource.CITIZEN,
        timestamp=datetime.utcnow(),
        raw_metadata={},
    )
    defaults.update(kwargs)
    return Incident(**defaults)


def test_weather_alert_high_credibility():
    inc = _incident(source=IncidentSource.WEATHER, id="weather-1")
    score, reasons = _score_incident(inc, [inc])
    assert score >= 0.9
    assert any("weather" in r.lower() for r in reasons)


def test_suspicious_report_low_credibility():
    inc = _incident(
        id="spam-1",
        text="FREE BITCOIN",
        lat=0.0,
        lng=0.0,
        raw_metadata={"suspicious": True},
    )
    score, reasons = _score_incident(inc, [inc])
    assert score < FLAG_THRESHOLD
    assert any("suspicious" in r.lower() for r in reasons)


def test_duplicate_reduces_credibility():
    a = _incident(id="r001", text="Water rising fast on Main St downtown", lat=29.7604, lng=-95.3698)
    b = _incident(id="r002", text="Water rising fast on Main St downtown area", lat=29.7605, lng=-95.3699)
    score, reasons = _score_incident(b, [a, b])
    assert score < 0.65
    assert any("duplicate" in r.lower() for r in reasons)
