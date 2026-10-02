"""Unit tests for rescue priority formula."""

from datetime import datetime, timedelta

from agents.rescue import compute_priority
from state import Incident, IncidentSource, NeedType, Urgency


def test_critical_rescue_with_vulnerable_ranks_higher():
    now = datetime.utcnow()
    critical = Incident(
        id="c1",
        text="Family on roof",
        location="Houston",
        lat=29.75,
        lng=-95.37,
        need_type=NeedType.RESCUE,
        urgency=Urgency.CRITICAL,
        source=IncidentSource.CITIZEN,
        timestamp=now - timedelta(hours=1),
        raw_metadata={"vulnerable": ["children", "elderly"]},
    )
    low = Incident(
        id="l1",
        text="Lots of rain",
        location="Houston",
        lat=29.76,
        lng=-95.38,
        need_type=NeedType.INFORMATION,
        urgency=Urgency.LOW,
        source=IncidentSource.CITIZEN,
        timestamp=now,
        raw_metadata={},
    )
    score_c, _ = compute_priority(critical, 0.9, now)
    score_l, _ = compute_priority(low, 0.9, now)
    assert score_c > score_l


def test_credibility_multiplier():
    now = datetime.utcnow()
    inc = Incident(
        id="x1",
        text="Need rescue",
        location="Houston",
        lat=29.76,
        lng=-95.37,
        need_type=NeedType.RESCUE,
        urgency=Urgency.HIGH,
        source=IncidentSource.CITIZEN,
        timestamp=now,
        raw_metadata={},
    )
    high, _ = compute_priority(inc, 0.9, now)
    low, _ = compute_priority(inc, 0.4, now)
    assert high > low
