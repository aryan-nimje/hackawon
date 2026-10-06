"""Pune layers, OSM cleaning, hospital diversion, event kinds, active run and report tagging."""

from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from agents.medical import _best_hospital, run_medical
from config import get_settings
from main import app
from services import osm
from services.bus import bus
from services.reports import report_store
from services.store import run_store
from state import Incident, IncidentSource, NeedType, Urgency, VerificationResult
from supervisor import create_run

client = TestClient(app)


# ── /layers ──────────────────────────────────────────────────────────────
def test_layers_default_is_pune():
    d = client.get("/layers").json()
    assert d["city"]["slug"] == "pune"
    assert abs(d["city"]["center"][0] - 18.52) < 0.1
    for key in ("hospitals", "depots", "flood_zones", "bridges", "fire_stations", "police_stations"):
        assert d[key], key
    h = d["hospitals"][0]
    assert {"id", "name", "lat", "lng", "beds", "specialties"} <= set(h)
    assert d["flood_zones"][0]["ring"][0] == d["flood_zones"][0]["ring"][-1]  # closed ring


def test_layers_uncached_city_404_when_live_osm_off(monkeypatch):
    monkeypatch.setenv("LIVE_OSM", "false")
    get_settings.cache_clear()
    try:
        assert client.get("/layers?city=Atlantis").status_code == 404
        assert client.get("/layers?city=Pune").status_code == 200
    finally:
        monkeypatch.delenv("LIVE_OSM")
        get_settings.cache_clear()


def test_agent_data_comes_from_pune_layers():
    from services.data_loader import load_blocked_zones, load_hospitals, load_inventory

    assert all(h["lat"] < 19 for h in load_hospitals())
    assert load_inventory()[0]["inventory"]["food_meals"] > 0
    assert load_blocked_zones()[0][0][0] > 70  # [lng, lat] order


# ── OSM cleaning (no network) ────────────────────────────────────────────
def _el(i, name, lat, lng, **tags):
    return {"type": "node", "id": i, "lat": lat, "lon": lng, "tags": {"name": name, **tags}}


def test_clean_hospitals_drops_unnamed_dupes_and_sets_beds():
    els = [
        _el(1, "City Hospital", 18.50, 73.85, beds="120"),
        _el(2, "City Hospital", 18.60, 73.90),  # same name -> dropped
        _el(3, "Other Clinic", 18.50001, 73.85001),  # within 150 m -> dropped
        {"type": "node", "id": 4, "lat": 18.4, "lon": 73.8, "tags": {"amenity": "hospital"}},  # unnamed
        _el(5, "Far Multispeciality Hospital", 18.7, 73.7, emergency="yes"),
    ]
    out = osm.clean_hospitals(els)
    assert [h["name"] for h in out] == ["City Hospital", "Far Multispeciality Hospital"]
    assert out[0]["beds"] == 120 and out[0]["beds_source"] == "osm"
    assert out[1]["beds_source"] == "generated" and 22 <= out[1]["beds"] <= 40
    assert out[1]["beds_available"] == out[1]["beds"]


def test_estimate_beds_is_deterministic():
    assert osm.estimate_beds({"name": "X Hospital"}, "node1") == osm.estimate_beds({"name": "X Hospital"}, "node1")


def test_clean_bridges_keeps_major_named_only():
    def way(name, hw):
        return {"tags": {"name": name, "highway": hw}, "geometry": [{"lat": 18.5, "lon": 73.8}, {"lat": 18.51, "lon": 73.8}]}

    out = osm.clean_bridges([way("A Bridge", "primary"), way("A Bridge", "primary"), way("Small", "residential"), way("", "primary")])
    assert [b["name"] for b in out] == ["A Bridge"]
    assert len(out[0]["geometry"]) == 2


def test_build_layers_generates_depots_and_flood_zones():
    city = {"name": "T", "slug": "t", "center": [18.5, 73.8], "zoom": 12, "bbox": [18, 73, 19, 74]}
    d = osm.build_layers(city, [_el(1, "H", 18.5, 73.8, amenity="hospital")], [])
    assert len(d["depots"]) == 4 and len(d["flood_zones"]) == 3 and d["hospitals"][0]["id"] == "h001"


# ── hospital diversion ───────────────────────────────────────────────────
def _h(i, lat, free, status="operational", spec=None):
    return {"id": i, "name": i.upper(), "lat": lat, "lng": 73.85, "beds_available": free, "status": status, "specialties": spec or []}


def _inc():
    return Incident(id="i1", text="chest pain", location="x", lat=18.50, lng=73.85, need_type=NeedType.MEDICAL,
                    urgency=Urgency.HIGH, source=IncidentSource.CITIZEN, timestamp=datetime(2026, 10, 2))


def test_nearest_with_beds_is_used():
    hosp, _, _, div, over = _best_hospital(_inc(), [_h("a", 18.51, 2), _h("b", 18.60, 5)], {})
    assert hosp["id"] == "a" and div is None and not over


def test_full_nearest_diverts_to_next_nearest_with_beds():
    hosp, _, _, div, over = _best_hospital(_inc(), [_h("a", 18.51, 1), _h("b", 18.55, 5), _h("c", 18.70, 9)], {"a": 1})
    assert hosp["id"] == "b" and div["id"] == "a" and not over


def test_status_full_and_offline():
    hosp, _, _, div, _ = _best_hospital(_inc(), [_h("a", 18.51, 9, status="full"), _h("b", 18.55, 5), _h("c", 18.52, 9, status="offline")], {})
    assert hosp["id"] == "b" and div["id"] == "a"  # c is offline so it is never even the nominal choice


def test_all_full_is_overflow_to_nearest_not_first_in_list():
    hosp, _, _, div, over = _best_hospital(_inc(), [_h("far", 18.90, 0), _h("near", 18.51, 0)], {})
    assert hosp["id"] == "near" and over and div is None


def test_no_operational_hospital_returns_none():
    assert _best_hospital(_inc(), [_h("a", 18.51, 5, status="offline")], {}) is None


async def test_run_medical_reports_diversions(monkeypatch):
    import agents.medical as med

    monkeypatch.setattr(med, "load_hospitals", lambda: [_h("a", 18.51, 1), _h("b", 18.55, 5)])
    run = create_run()
    for n in (1, 2):
        run.incidents.append(_inc().model_copy(update={"id": f"i{n}"}))
        run.verifications.append(VerificationResult(incident_id=f"i{n}", credibility=0.9, reasons=[]))
    out = await run_medical(run)
    assert [a.hospital_id for a in out.assignments] == ["a", "b"]
    assert out.assignments[1].diverted_from == "A"


# ── sim events ───────────────────────────────────────────────────────────
@pytest.mark.parametrize("kind", ["hospital_full", "patient_diverted"])
def test_new_sim_event_kinds_accepted(kind):
    assert client.post("/sim/event", json={"kind": kind, "text": "x", "at": 1}).status_code == 200


# ── active run + reports ─────────────────────────────────────────────────
_SUB = {"text": "Water in my house, need help please", "lat": 18.52, "lng": 73.85}


def test_active_run_endpoint_and_world_tagging():
    run = create_run()
    run_store.set_active(run.id)
    assert client.get("/runs/active").json()["run_id"] == run.id
    w = {"vehicles": [], "disruptions": [], "affected_regions": [], "sim_incidents": [], "execution": [], "events": [],
         "beds": {}, "urgency_override": {}, "sim_time_s": 1, "ts": 1}
    assert client.post("/sim/world", json=w).status_code == 200
    assert bus.latest_world["run_id"] == run.id
    assert client.post("/sim/world", json={**w, "run_id": "other"}).status_code == 200
    assert bus.latest_world["run_id"] == "other"  # an explicit tag is kept


def test_reports_are_tagged_with_active_run_and_old_runs_hidden():
    r1, r2 = create_run(), create_run()
    run_store.set_active(r1.id)
    t1 = client.post("/reports", json=_SUB).json()["token"]
    run_store.set_active(r2.id)
    t2 = client.post("/reports", json=_SUB).json()["token"]
    tokens = {r["token"] for r in client.get("/reports").json()}
    assert t2 in tokens and t1 not in tokens  # old run hidden
    assert t1 in {r["token"] for r in client.get("/reports?all=true").json()}  # but not deleted
    assert report_store.get(t1)["run_id"] == r1.id


def test_untagged_reports_always_visible():
    run_store.active_run_id = None
    t = client.post("/reports", json=_SUB).json()["token"]
    run_store.set_active(create_run().id)
    assert t in {r["token"] for r in client.get("/reports").json()}


# ── weather / verification are city aware ────────────────────────────────
async def test_weather_alert_uses_pune_in_mock_mode():
    from services.weather import fetch_weather_alert

    inc = await fetch_weather_alert()
    assert inc is not None and "Pune" in inc.location and abs(inc.lat - 18.52) < 0.1
    assert "Houston" not in inc.text and "Harris" not in inc.text


def test_verification_flags_location_outside_pune():
    from agents.verification import _score_incident

    inside = _inc()
    outside = _inc().model_copy(update={"lat": 29.76, "lng": -95.37})
    s_in, _ = _score_incident(inside, [inside])
    s_out, reasons = _score_incident(outside, [outside])
    assert s_out < s_in and any("Pune" in r for r in reasons)
