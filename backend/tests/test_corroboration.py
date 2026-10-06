"""Verification: rule-based support from external signals and crowd corroboration."""

import random
from datetime import datetime, timedelta

import pytest

from agents.verification import _score_incident, run_verification
from services import corroboration as cor
from services.signals.models import Signal, SignalKind, SignalSource, SignalStatus
from state import Incident, IncidentSource, NeedType, RunState, Urgency

LAT, LNG = 18.5130, 73.8455  # Deccan, Pune (inside the default city box)
NOW = datetime(2026, 10, 2, 10, 0, 0)
# Pune district-sized polygon around the incident ([lat, lng] ring), ~60 km across
DISTRICT = [[18.2, 73.5], [18.2, 74.2], [18.8, 74.2], [18.8, 73.5]]
# A ~2 km local polygon around the incident
LOCAL = [[LAT - 0.01, LNG - 0.01], [LAT - 0.01, LNG + 0.01], [LAT + 0.01, LNG + 0.01], [LAT + 0.01, LNG - 0.01]]


def inc(id="i1", text="Water rising fast on my street", lat=LAT, lng=LNG, ts=NOW, source=IncidentSource.CITIZEN,
        need=NeedType.SHELTER, urgency=Urgency.MEDIUM, location="Deccan Gymkhana, Pune", **meta):
    return Incident(id=id, text=text, location=location, lat=lat, lng=lng, need_type=need, urgency=urgency,
                    source=source, timestamp=ts, raw_metadata=meta)


def sig(id="sachet:1", source=SignalSource.SACHET, kind=SignalKind.OFFICIAL_ALERT, hazards=("flood", "rain"),
        title="Heavy rain warning for Pune", polygons=None, bbox=None, issued=NOW - timedelta(hours=1),
        expires=NOW + timedelta(hours=12), weight=0.6, url="https://sachet.example/cap?identifier=1", **kw):
    polygons = [DISTRICT] if polygons is None else polygons
    if bbox is None and polygons:
        bbox = [min(p[0] for r in polygons for p in r), min(p[1] for r in polygons for p in r),
                max(p[0] for r in polygons for p in r), max(p[1] for r in polygons for p in r)]
    return Signal(id=id, source=source, kind=kind, title=title, hazards=list(hazards), polygons=polygons, bbox=bbox,
                  issued_at=issued, expires_at=expires, weight=weight, trust=0.9, source_url=url, **kw)


def score(i, others=(), signals=()):
    return _score_incident(i, [i, *others], signals=list(signals))


BASE = 0.65


# -- evidence: compatibility ---------------------------------------------------------------------------------
def test_flood_alert_never_supports_fire_incident():
    fire = inc(text="Building on fire near Deccan, flames spreading", )
    s, reasons = score(fire, signals=[sig(hazards=("flood", "rain"))])
    assert s == pytest.approx(BASE)
    assert not any("Supported by" in r for r in reasons)


def test_fire_incident_mentioning_rain_is_still_fire_only():
    fire = inc(text="Fire in a shop, firefighters stuck in the rain")
    assert cor.incident_hazards(fire) == {"fire"}
    assert score(fire, signals=[sig(hazards=("rain",))])[0] == pytest.approx(BASE)


def test_fire_alert_supports_fire_but_not_flood():
    fire_alert = sig(id="sachet:fire", hazards=("fire",), title="Fire warning")
    assert score(inc(text="Fire in a godown"), signals=[fire_alert])[0] == pytest.approx(BASE + 0.05)
    assert score(inc(text="Flooded street"), signals=[fire_alert])[0] == pytest.approx(BASE)


def test_rain_alert_supports_flood_report_and_unrelated_text_gets_nothing():
    assert score(inc(text="Street flooded"), signals=[sig()])[0] == pytest.approx(BASE + 0.05)
    assert score(inc(text="Need baby formula, shops closed"), signals=[sig()])[0] == pytest.approx(BASE)


# -- evidence: broad vs local, no penalty --------------------------------------------------------------------
def test_no_matching_signal_means_no_penalty():
    s, reasons = score(inc(), signals=[])
    assert s == pytest.approx(BASE)
    assert reasons == ["Standard citizen report with no corroboration yet"]


def test_district_wide_alert_gives_small_boost_local_evidence_larger():
    broad = score(inc(), signals=[sig()])[0]
    local_area = score(inc(), signals=[sig(polygons=[LOCAL])])[0]
    assert broad == pytest.approx(BASE + 0.05)
    assert local_area == pytest.approx(BASE + 0.12)
    assert local_area > broad


def test_news_naming_the_locality_is_local_otherwise_city_wide():
    city_box = [18.40, 73.70, 18.65, 74.0]
    generic = sig(id="gdelt:a", source=SignalSource.GDELT, kind=SignalKind.NEWS, polygons=[], bbox=city_box,
                  title="Heavy rain lashes Pune, roads waterlogged")
    named = sig(id="gdelt:b", source=SignalSource.GDELT, kind=SignalKind.NEWS, polygons=[], bbox=city_box,
                title="Deccan Gymkhana waterlogged after heavy rain")
    assert score(inc(), signals=[generic])[0] == pytest.approx(BASE + 0.05)
    assert score(inc(), signals=[named])[0] == pytest.approx(BASE + 0.12)


def test_river_gauge_is_local_within_radius_only():
    gauge = dict(polygons=[], bbox=None, lat=LAT + 0.005, lng=LNG, metadata={"type": "river_gauge"})
    near = sig(id="gauge:1", hazards=("flood",), **gauge)
    far = sig(id="gauge:2", hazards=("flood",), **{**gauge, "lat": LAT + 0.5})
    assert score(inc(), signals=[near])[0] == pytest.approx(BASE + 0.12)
    assert score(inc(), signals=[far])[0] == pytest.approx(BASE)


def test_distance_outside_area_gives_nothing():
    far_polygon = [[19.5, 74.5], [19.5, 74.8], [19.8, 74.8], [19.8, 74.5]]
    assert score(inc(), signals=[sig(polygons=[far_polygon])])[0] == pytest.approx(BASE)


def test_extra_source_adds_small_bonus_and_is_capped():
    a = sig()
    b = sig(id="open_meteo:pune:rain", source=SignalSource.OPEN_METEO, kind=SignalKind.WEATHER, polygons=[],
            bbox=[18.40, 73.70, 18.65, 74.0], url="https://open-meteo.com/")
    assert score(inc(), signals=[a, b])[0] == pytest.approx(BASE + 0.05 + 0.03)
    assert score(inc(), signals=[a, a.model_copy(update={"id": "sachet:2"})])[0] == pytest.approx(BASE + 0.05)


# -- evidence: time, status, strength ------------------------------------------------------------------------
def test_time_window():
    assert score(inc(), signals=[sig(issued=NOW - timedelta(days=3), expires=NOW - timedelta(days=2))])[0] == pytest.approx(BASE)
    just_expired = sig(issued=NOW - timedelta(hours=8), expires=NOW - timedelta(hours=1))
    assert score(inc(), signals=[just_expired])[0] == pytest.approx(BASE + 0.05)  # within the grace period
    later = sig(issued=NOW + timedelta(hours=2), expires=NOW + timedelta(hours=20))
    assert score(inc(), signals=[later])[0] == pytest.approx(BASE + 0.05)  # report shortly before the alert
    way_later = sig(issued=NOW + timedelta(days=2), expires=NOW + timedelta(days=3))
    assert score(inc(), signals=[way_later])[0] == pytest.approx(BASE)


def test_aware_incident_timestamp_is_handled():
    from datetime import timezone
    aware = inc(ts=NOW.replace(tzinfo=timezone.utc))
    assert score(aware, signals=[sig()])[0] == pytest.approx(BASE + 0.05)


def test_cancelled_and_weak_signals_are_ignored():
    assert score(inc(), signals=[sig(status=SignalStatus.CANCELLED)])[0] == pytest.approx(BASE)
    assert score(inc(), signals=[sig(status=SignalStatus.SUPERSEDED)])[0] == pytest.approx(BASE)
    assert score(inc(), signals=[sig(weight=0.05)])[0] == pytest.approx(BASE)


def test_support_reason_wording_names_source_and_link_and_never_says_verified():
    s, reasons = score(inc(), signals=[sig()])
    support = [r for r in reasons if r.startswith("Supported by")]
    assert len(support) == 1
    assert "SACHET" in support[0] and "https://sachet.example/cap?identifier=1" in support[0]
    assert not any("verified" in r.lower() for r in reasons)


def test_suspicious_or_implausible_reports_are_not_lifted():
    assert score(inc(suspicious=True), signals=[sig()])[0] < 0.45
    assert score(inc(lat=0.0, lng=0.0), signals=[sig()])[0] < 0.45


def test_weather_and_news_incidents_are_not_boosted_by_signals():
    w = inc(id="w", source=IncidentSource.WEATHER)
    assert score(w, signals=[sig()])[0] == pytest.approx(0.95)


def test_malformed_signal_does_not_break_scoring():
    bad = sig().model_copy(update={"bbox": ["x"], "polygons": [], "circles": [[1.0]]})
    assert score(inc(), signals=[bad])[0] == pytest.approx(BASE)


# -- crowd ------------------------------------------------------------------------------------------------
def test_crowd_boost_steps_and_cap():
    steps = {1: 0.0, 2: 0.10, 3: 0.15, 4: 0.15, 5: 0.20, 9: 0.20, 10: 0.25, 24: 0.25, 25: 0.35, 26: 0.35, 400: 0.35}
    for n, expected in steps.items():
        assert cor.crowd_boost_for(n) == pytest.approx(expected), n


def _distinct(n, base_ts=NOW, step_min=5):
    """n genuinely different reports: different words, ~44 m apart, minutes apart."""
    rnd = random.Random(7)
    out = []
    for k in range(n):
        words = " ".join("".join(rnd.choice("abcdefghijklmnopqrstuvwxyz") for _ in range(6)) for _ in range(5))
        out.append(inc(id=f"c{k}", text=words, lat=LAT + (k // 5) * 0.0004, lng=LNG + (k % 5) * 0.0004,
                       ts=base_ts + timedelta(minutes=step_min * k)))
    return out


@pytest.mark.parametrize("n,boost", [(1, 0.0), (2, 0.10), (3, 0.15), (5, 0.20), (10, 0.25), (25, 0.35), (30, 0.35)])
def test_independent_reports_give_tiered_boost(n, boost):
    crowd = _distinct(n)
    s, reasons = _score_incident(crowd[0], crowd, signals=[])
    assert s == pytest.approx(min(0.95, BASE + boost))  # 25+ reports: 0.65 + 0.35 would be 1.0, held at the ceiling
    if boost:
        assert any(r.startswith("Corroborated by") and f"+{boost:.2f}" in r for r in reasons)


def test_crowd_size_stays_uncapped_for_severity():
    crowd = _distinct(30)
    c = cor.assess_crowd(crowd[0], crowd)
    assert c.size == 30 and c.boost == pytest.approx(0.35)


def test_copy_pasted_reports_count_once():
    text = "Water rising fast on my street! 2 feet deep already near Lakdi Pul, Deccan Gymkhana"
    a = inc(id="a", text=text)
    b = inc(id="b", text=text.replace("Gymkhana", ""), lat=LAT + 0.0003, ts=NOW + timedelta(minutes=10))
    c = inc(id="c", text=text.upper(), lat=LAT - 0.0004, ts=NOW + timedelta(minutes=30))
    assert cor.assess_crowd(a, [a, b, c]).size == 1
    assert score(a, [b, c], signals=[])[0] == pytest.approx(BASE)
    others = _distinct(2, base_ts=NOW + timedelta(hours=1))
    assert cor.assess_crowd(a, [a, b, c, *others]).size == 3  # copies merged, two different reports added


def test_same_spot_same_moment_counts_once_even_with_different_words():
    a = inc(id="a", text="qwerty uiop asdf ghjk")
    b = inc(id="b", text="zxcv bnm lkjh poiu", ts=NOW + timedelta(seconds=30))  # same coordinates, 30 s apart
    assert cor.assess_crowd(a, [a, b]).size == 1
    c = inc(id="c", text="zxcv bnm lkjh poiu", ts=NOW + timedelta(minutes=20))  # same spot, much later, other words
    assert cor.assess_crowd(a, [a, c]).size == 2


def test_similar_text_from_same_spot_is_one_report():
    a = inc(id="a", text="water in the basement need help please")
    b = inc(id="b", text="water in basement, need help", ts=NOW + timedelta(minutes=30))
    assert 0.6 <= cor.text_similarity(a.text, b.text) < 0.85
    assert cor.assess_crowd(a, [a, b]).size == 1


def test_only_nearby_recent_same_kind_citizen_reports_count():
    base = inc(id="a", text="qwerty uiop flooded street ghjk")
    far = inc(id="far", text="zxcv bnm lkjh poiu", lat=LAT + 0.05, ts=NOW + timedelta(minutes=10))  # ~5.5 km
    old = inc(id="old", text="mnbv cxz lkjh gfds", ts=NOW - timedelta(hours=12), lat=LAT + 0.001)
    fire = inc(id="fire", text="Fire in the godown", lat=LAT + 0.002, ts=NOW + timedelta(minutes=12))
    flood = inc(id="flood", text="Street is flooded knee deep", lat=LAT + 0.003, ts=NOW + timedelta(minutes=14))
    sim = inc(id="sim", text="poiuy trewq lkjhg mnbvc", source=IncidentSource.SIM, lat=LAT + 0.004, ts=NOW + timedelta(minutes=16))
    spam = inc(id="spam", text="rtyu fghj vbnm sdfg", lat=LAT + 0.005, ts=NOW + timedelta(minutes=18), suspicious=True)
    assert cor.assess_crowd(base, [base, far, old, fire, sim, spam]).size == 1
    assert cor.assess_crowd(base, [base, flood]).size == 2


def test_reporter_identity_is_never_used():
    a = inc(id="a", text="qwerty uiop asdf ghjk", reporter_id="u1", phone="999")
    b = inc(id="b", text="zxcv bnm lkjh poiu", reporter_id="u1", phone="999", lat=LAT + 0.001, ts=NOW + timedelta(minutes=9))
    assert cor.assess_crowd(a, [a, b]).size == 2


def test_evidence_and_crowd_add_but_credibility_never_exceeds_ceiling():
    crowd = _distinct(30)
    first = crowd[0].model_copy(update={"urgency": Urgency.CRITICAL, "need_type": NeedType.RESCUE})
    s, _ = _score_incident(first, [first, *crowd[1:]], signals=[sig(polygons=[LOCAL]), sig(id="n", source=SignalSource.GDELT)])
    assert s == 0.95
    s2, _ = _score_incident(crowd[0], crowd, signals=[sig(polygons=[LOCAL])])  # no flood word: no evidence, crowd only
    assert s2 == 0.95


# -- configuration ----------------------------------------------------------------------------------------
def _set(monkeypatch, value):
    from config import get_settings
    monkeypatch.setenv("CORROBORATION_CONFIG", value)
    get_settings.cache_clear()


def test_config_override_and_validation(monkeypatch):
    _set(monkeypatch, '{"evidence_boost_broad": 0.08, "crowd_boost_tiers": [[2, 0.2], [4, 0.3]]}')
    assert score(inc(), signals=[sig()])[0] == pytest.approx(BASE + 0.08)
    assert cor.crowd_boost_for(2) == 0.2 and cor.crowd_boost_for(4) == 0.3 and cor.crowd_boost_for(50) == 0.3
    _set(monkeypatch, '{"evidence_boost_broad": 7, "bogus": 1, "crowd_boost_tiers": [[3, 0.2], [2, 0.3]]}')
    assert cor.config() == cor.CorroborationConfig()
    _set(monkeypatch, "not json")
    assert cor.config() == cor.CorroborationConfig()


def test_ceiling_can_be_lowered_but_not_raised(monkeypatch):
    _set(monkeypatch, '{"max_credibility": 0.99}')
    assert cor.credibility_ceiling() == 0.95
    _set(monkeypatch, '{"max_credibility": 0.7}')
    assert cor.credibility_ceiling() == 0.7
    assert score(inc(), signals=[sig(polygons=[LOCAL])])[0] == pytest.approx(0.7)


# -- run_verification keeps crowd size and support alongside the score -----------------------------------------
async def test_run_verification_exposes_crowd_size_and_supporting_signal_ids():
    from services.signals.store import signal_store

    flood_sig = sig(issued=datetime.utcnow() - timedelta(hours=1), expires=datetime.utcnow() + timedelta(hours=6))
    signal_store.upsert_many([flood_sig])
    now = datetime.utcnow()
    crowd = _distinct(5, base_ts=now - timedelta(hours=1))
    crowd[0] = crowd[0].model_copy(update={"text": "Water rising fast on my street"})
    state = RunState(id="r", incidents=crowd)
    out = (await run_verification(state)).results
    first = next(r for r in out if r.incident_id == "c0")
    assert first.crowd_size == 5
    assert first.supported_by == [flood_sig.id]
    assert first.credibility == pytest.approx(round(BASE + 0.05 + 0.20, 2))
    assert any("Supported by" in r for r in first.reasons)
