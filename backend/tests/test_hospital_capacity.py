"""Live hospital capacity: the simulator's census drives the Medical Agent, so occupancy, admission,
discharge and overflow agree between the maps and the plan."""

from datetime import datetime

import pytest
from fastapi.testclient import TestClient

import agents.medical as med
from agents.medical import _expected_stay_min, run_medical
from main import app
from services.bus import bus
from services.hospital_state import hospital_store
from services.store import run_store
from state import (
    HospitalAssignment, Incident, IncidentSource, NeedType, PlanItem, PlanItemStatus, ResponsePlan, Urgency,
    VerificationResult,
)
from supervisor import create_run

client = TestClient(app)


@pytest.fixture(autouse=True)
def _clean_store():
    hospital_store.reset()
    yield
    hospital_store.reset()


def _h(i, lat, free=10, status="operational"):
    return {"id": i, "name": i.upper(), "lat": lat, "lng": 73.85, "beds": 10, "beds_available": free, "status": status, "specialties": []}


def _inc(n=1, urgency=Urgency.HIGH, **meta):
    return Incident(id=f"i{n}", text="chest pain", location="x", lat=18.50, lng=73.85, need_type=NeedType.MEDICAL,
                    urgency=urgency, source=IncidentSource.CITIZEN, timestamp=datetime(2026, 10, 2), raw_metadata=meta)


def _census(**hospitals):
    """{id: (capacity, occupied[, status])} -> a world payload carrying the census."""
    load = {}
    for hid, v in hospitals.items():
        cap, occ, *rest = v
        load[hid] = {"capacity": cap, "occupied": occ, "free": cap - occ, "status": rest[0] if rest else "open"}
    return {"vehicles": [], "disruptions": [], "affected_regions": [], "sim_incidents": [], "execution": [], "events": [],
            "beds": {k: v["free"] for k, v in load.items()}, "urgency_override": {}, "sim_time_s": 5, "ts": 1,
            "hospital_load": load, "admitted_incident_ids": []}


def _post(world, **extra):
    assert client.post("/sim/world", json={**world, **extra}).status_code == 200


def _run_with(n=2):
    run = create_run()
    for k in range(1, n + 1):
        run.incidents.append(_inc(k))
        run.verifications.append(VerificationResult(incident_id=f"i{k}", credibility=0.9, reasons=[]))
    return run


# ── ingest ───────────────────────────────────────────────────────────────
def test_world_post_updates_store_and_is_forwarded():
    _post(_census(a=(10, 4), b=(10, 10)), run_id=None)
    assert hospital_store.get("a")["free"] == 6 and hospital_store.get("b")["free"] == 0
    assert bus.latest_world["hospital_load"]["a"]["occupied"] == 4  # other clients see the same census


def test_world_without_census_clears_live_view():
    _post(_census(a=(10, 4)))
    assert hospital_store.is_live()
    _post({k: v for k, v in _census(a=(10, 4)).items() if k not in ("hospital_load", "admitted_incident_ids")})
    assert not hospital_store.is_live()
    assert "hospital_load" not in bus.latest_world  # absent, not null


def test_overlay_uses_live_free_beds_and_status():
    _post(_census(a=(10, 10), b=(10, 3, "full")), run_id=None)
    out = {h["id"]: h for h in hospital_store.overlay([_h("a", 18.51), _h("b", 18.52), _h("c", 18.53)])}
    assert out["a"]["beds_available"] == 0 and out["a"]["status"] == "operational"
    assert out["b"]["status"] == "full"
    assert out["c"]["beds_available"] == 10  # not in the census: static data


def test_census_of_another_run_is_ignored():
    run = create_run()
    _post(_census(a=(10, 10)), run_id="some-other-run")
    assert not hospital_store.is_live(run.id)
    assert hospital_store.overlay([_h("a", 18.51)], run.id)[0]["beds_available"] == 10


# ── Medical Agent plans against live occupancy ───────────────────────────
async def test_agent_diverts_when_live_census_says_nearest_is_full(monkeypatch):
    monkeypatch.setattr(med, "load_hospitals", lambda: [_h("a", 18.51, free=10), _h("b", 18.55, free=10)])
    run = _run_with(1)
    _post(_census(a=(10, 10), b=(10, 2)), run_id=run.id)  # static data says A has 10 free; the census says 0
    out = await run_medical(run)
    a = out.assignments[0]
    assert a.hospital_id == "b" and a.diverted_from == "A" and not a.overflow


async def test_agent_overflows_when_census_says_everything_is_full(monkeypatch):
    monkeypatch.setattr(med, "load_hospitals", lambda: [_h("a", 18.51), _h("b", 18.55)])
    run = _run_with(1)
    _post(_census(a=(10, 10), b=(10, 10)), run_id=run.id)
    a = (await run_medical(run)).assignments[0]
    assert a.hospital_id == "a" and a.overflow


async def test_bed_returns_after_discharge(monkeypatch):
    """The same case: while the stay lasts the hospital is full and the patient is diverted; after discharge it is not."""
    monkeypatch.setattr(med, "load_hospitals", lambda: [_h("a", 18.51), _h("b", 18.55)])
    run = _run_with(1)
    _post(_census(a=(10, 10), b=(10, 2)), run_id=run.id)
    assert (await run_medical(run)).assignments[0].hospital_id == "b"
    _post(_census(a=(10, 9), b=(10, 2)), run_id=run.id)  # one stay ended
    after = (await run_medical(run)).assignments[0]
    assert after.hospital_id == "a" and after.diverted_from is None


async def test_assigned_but_not_admitted_patients_reserve_beds(monkeypatch):
    monkeypatch.setattr(med, "load_hospitals", lambda: [_h("a", 18.51), _h("b", 18.55)])
    run = _run_with(2)
    _post(_census(a=(10, 9), b=(10, 2)), run_id=run.id)  # A has one bed free
    run.hospital_assignments = [HospitalAssignment(incident_id="i1", hospital_id="a", hospital_name="A", distance_km=1, explanation="")]
    out = await run_medical(run, only_ids={"i2"})
    assert out.assignments[0].hospital_id == "b"  # i1 is on the way to A's last bed


async def test_admitted_patients_are_not_counted_twice(monkeypatch):
    monkeypatch.setattr(med, "load_hospitals", lambda: [_h("a", 18.51), _h("b", 18.55)])
    run = _run_with(2)
    # i1 is already in a bed at A (it is in the occupancy: 9/10) and listed as admitted
    _post({**_census(a=(10, 9), b=(10, 2)), "admitted_incident_ids": ["i1"]}, run_id=run.id)
    run.hospital_assignments = [HospitalAssignment(incident_id="i1", hospital_id="a", hospital_name="A", distance_km=1, explanation="")]
    out = await run_medical(run, only_ids={"i2"})
    assert out.assignments[0].hospital_id == "a"  # one bed really is free; i1 must not take it again


async def test_discharged_incident_patient_holds_nothing(monkeypatch):
    monkeypatch.setattr(med, "load_hospitals", lambda: [_h("a", 18.51), _h("b", 18.55)])
    run = _run_with(2)
    _post({**_census(a=(10, 9), b=(10, 2)), "admitted_incident_ids": ["i1"]}, run_id=run.id)  # i1 admitted earlier, already discharged
    run.hospital_assignments = [HospitalAssignment(incident_id="i1", hospital_id="a", hospital_name="A", distance_km=1, explanation="")]
    assert (await run_medical(run, only_ids={"i2"})).assignments[0].hospital_id == "a"


async def test_rejected_item_releases_its_reservation(monkeypatch):
    monkeypatch.setattr(med, "load_hospitals", lambda: [_h("a", 18.51), _h("b", 18.55)])
    run = _run_with(2)
    _post(_census(a=(10, 9), b=(10, 2)), run_id=run.id)
    run.hospital_assignments = [HospitalAssignment(incident_id="i1", hospital_id="a", hospital_name="A", distance_km=1, explanation="")]
    run.plan = ResponsePlan(run_id=run.id, items=[PlanItem(id="medical-i1", category="medical", title="t", description="d",
                                                          reasoning="r", status=PlanItemStatus.REJECTED)])
    assert (await run_medical(run, only_ids={"i2"})).assignments[0].hospital_id == "a"


async def test_without_a_census_behaviour_is_unchanged(monkeypatch):
    monkeypatch.setattr(med, "load_hospitals", lambda: [_h("a", 18.51, free=1), _h("b", 18.55, free=5)])
    out = await run_medical(_run_with(2))
    assert [a.hospital_id for a in out.assignments] == ["a", "b"] and out.assignments[1].diverted_from == "A"


# ── length of stay ───────────────────────────────────────────────────────
def test_every_assignment_has_a_length_of_stay():
    assert _expected_stay_min(_inc()) > 0
    assert _expected_stay_min(_inc()) == _expected_stay_min(_inc())  # deterministic per incident


def test_stay_grows_with_urgency_and_vulnerability():
    low = _expected_stay_min(_inc(urgency=Urgency.LOW))
    crit = _expected_stay_min(_inc(urgency=Urgency.CRITICAL))
    assert 20 <= low <= 45 and 90 <= crit <= 150 and crit > low
    assert _expected_stay_min(_inc(vulnerable=["infant"])) > _expected_stay_min(_inc()) * 1.1


async def test_assignments_carry_expected_stay(monkeypatch):
    monkeypatch.setattr(med, "load_hospitals", lambda: [_h("a", 18.51)])
    out = await run_medical(_run_with(2))
    assert all(a.expected_stay_min and a.expected_stay_min > 0 for a in out.assignments)


# ── supervisor announcements and endpoint ────────────────────────────────
def test_capacity_announcements_use_live_beds(monkeypatch):
    import supervisor

    monkeypatch.setattr(supervisor, "load_hospitals", lambda: [_h("a", 18.51, free=10)])
    sent = []
    monkeypatch.setattr(supervisor.bus, "publish", lambda kind, data: sent.append((kind, data)))
    run = create_run()
    asg = HospitalAssignment(incident_id="i1", hospital_id="a", hospital_name="A", distance_km=1, explanation="")
    supervisor._announce_capacity(run.id, [asg])
    full = lambda: [d for name, d in sent if name == "sim_event" and d["kind"] == "hospital_full"]  # noqa: E731
    assert not full()  # static data: 10 beds, one patient
    _post(_census(a=(10, 9)), run_id=run.id)  # one bed really free
    supervisor._announce_capacity(run.id, [asg])
    assert full()


def test_capacity_endpoint_static_then_live():
    d = client.get("/hospitals/capacity").json()
    assert d["live"] is False and d["hospitals"] and all(h["occupied"] == 0 for h in d["hospitals"])
    first = d["hospitals"][0]["id"]
    run = create_run()
    run_store.set_active(run.id)
    _post(_census(**{first: (10, 7)}), run_id=run.id)
    d = client.get("/hospitals/capacity").json()
    h = next(x for x in d["hospitals"] if x["id"] == first)
    assert d["live"] is True and (h["capacity"], h["occupied"], h["free"]) == (10, 7, 3) and h["occupancy"] == 0.7
