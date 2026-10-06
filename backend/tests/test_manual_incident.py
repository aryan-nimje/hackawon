"""Manual incidents (POST /incidents) are planned incrementally through the normal agent workflow."""

import asyncio
import time
from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from main import app
from services.store import run_store
from state import Incident, IncidentSource, NeedType, PlanItemStatus, Urgency
from supervisor import create_run, plan_new_incident, register_incident, run_pipeline


def _incident(id_: str, need: NeedType, lat: float, lng: float, urgency: Urgency = Urgency.HIGH) -> Incident:
    return Incident(
        id=id_, text="Family trapped on the first floor, water rising", location="Test St", lat=lat, lng=lng,
        need_type=need, urgency=urgency, source=IncidentSource.SIM, timestamp=datetime.utcnow(),
        raw_metadata={"manual": True},
    )


async def _planned_run():
    run = create_run()
    await run_pipeline(run)
    assert run.plan is not None
    return run


async def _add(run, inc):
    await register_incident(run, inc)
    await plan_new_incident(run.id, inc.id)


async def test_rescue_incident_is_added_without_resetting_the_plan():
    run = await _planned_run()
    before = {i.id: i for i in run.plan.items}
    # approve everything that exists: the plan is final and some "vehicles" would be dispatched
    for i in run.plan.items:
        i.status = PlanItemStatus.APPROVED
    run.plan.is_final = True
    n_before = len(run.plan.items)
    zones_before = {z.id for z in run.zones}

    c_lat, c_lng = run.incidents[0].lat, run.incidents[0].lng
    inc = _incident("manual-r1", NeedType.RESCUE, c_lat + 0.001, c_lng, Urgency.CRITICAL)
    inc.raw_metadata["vulnerable"] = ["infant", "children", "elderly"]  # high priority, so it ranks near the top
    await _add(run, inc)

    ids = {i.id for i in run.plan.items}
    assert set(before) <= ids  # nothing was removed
    assert all(i.status == PlanItemStatus.APPROVED for i in run.plan.items if i.id in before)  # approvals kept
    assert "rescue-manual-r1" in ids and "route-manual-r1-rescue" in ids
    new = [i for i in run.plan.items if i.id not in before]
    assert new and all(i.status == PlanItemStatus.PENDING for i in new)
    assert run.plan.is_final is False and run.plan.approved_at is None  # new items need Authority approval
    assert zones_before <= {z.id for z in run.zones}  # existing zone ids untouched
    assert any(v.incident_id == "manual-r1" for v in run.verifications)
    assert any(r.incident_id == "manual-r1" for r in run.plan.rescue_queue)
    assert len(run.plan.items) > n_before
    # an existing route object is kept as is
    assert all(any(r.assignment_id == b.assignment_id and r.assignment_type == b.assignment_type for r in run.routes)
               for b in run.routes)


async def test_medical_incident_gets_hospital_and_route():
    run = await _planned_run()
    inc = _incident("manual-m1", NeedType.MEDICAL, run.incidents[0].lat, run.incidents[0].lng + 0.002)
    await _add(run, inc)
    ids = {i.id for i in run.plan.items}
    assert "medical-manual-m1" in ids and "route-manual-m1-medical" in ids
    assert any(h.incident_id == "manual-m1" for h in run.hospital_assignments)


async def test_hospital_capacity_counts_existing_assignments(monkeypatch):
    from agents import medical

    hospitals = [
        {"id": "h1", "name": "Near", "lat": 18.5, "lng": 73.8, "beds_available": 1, "specialties": []},
        {"id": "h2", "name": "Far", "lat": 18.6, "lng": 73.9, "beds_available": 5, "specialties": []},
    ]
    monkeypatch.setattr(medical, "load_hospitals", lambda: hospitals)
    run = create_run()
    run.verifications = []
    a = _incident("m-a", NeedType.MEDICAL, 18.5, 73.8)
    b = _incident("m-b", NeedType.MEDICAL, 18.5, 73.8)
    run.incidents = [a, b]
    from agents.verification import run_verification

    run.verifications = (await run_verification(run)).results
    first = await medical.run_medical(run, {"m-a"})
    run.hospital_assignments = first.assignments
    second = await medical.run_medical(run, {"m-b"})
    assert first.assignments[0].hospital_id == "h1"
    assert second.assignments[0].hospital_id == "h2" and second.assignments[0].diverted_from == "Near"


async def test_new_zone_gets_supplies_from_remaining_stock():
    run = await _planned_run()
    far = _incident("manual-far", NeedType.SHELTER, 18.9, 73.2)  # far from every existing zone
    known = {z.id for z in run.zones}
    await _add(run, far)
    new_zones = {z.id for z in run.zones} - known
    assert len(new_zones) == 1
    assert any(f"logistics-{z}" in {i.id for i in run.plan.items} for z in new_zones)


async def test_agent_failure_falls_back_to_incomplete_placeholder():
    run = create_run(simulate_failures=["rescue"])
    await run_pipeline(run)
    inc = _incident("manual-f1", NeedType.RESCUE, run.incidents[0].lat, run.incidents[0].lng)
    await _add(run, inc)
    assert "rescue" in run.incomplete_sections
    assert any(i.incomplete for i in run.plan.items if i.category == "rescue")


async def test_incident_added_while_pipeline_runs_is_planned_once():
    run = create_run()
    task = asyncio.create_task(run_pipeline(run))
    await asyncio.sleep(0)  # pipeline is now in flight and holds the run lock
    inc = _incident("manual-race", NeedType.RESCUE, 18.52, 73.85)
    await register_incident(run, inc)
    await plan_new_incident(run.id, inc.id)
    await task
    await plan_new_incident(run.id, inc.id)  # a repeat is harmless
    items = [i.id for i in run.plan.items]
    assert items.count("rescue-manual-race") == 1
    assert len(items) == len(set(items))
    assert sum(1 for r in run.rescue_queue if r.incident_id == "manual-race") == 1


def test_api_creates_incident_in_run_and_plans_it():
    with TestClient(app) as client:
        started = client.post("/scenario/start", json={"replay_speed": 10})
        run_id = started.json()["run_id"]
        for _ in range(400):
            run = run_store.get_run(run_id)
            if run and run.plan:
                break
            time.sleep(0.1)
        assert run.plan is not None
        res = client.post("/incidents", json={
            "text": "Elderly neighbour cannot leave the house", "lat": 18.52, "lng": 73.85,
            "need_type": "rescue", "urgency": "critical", "people": 2, "run_id": run_id,
        })
        assert res.status_code == 201
        body = res.json()
        assert body["source"] == "sim" and body["run_id"] == run_id and body["id"].startswith("manual-")
        for _ in range(100):
            if any(i.id == f"rescue-{body['id']}" for i in run_store.get_run(run_id).plan.items):
                break
            time.sleep(0.1)
        plan = client.get(f"/plan/{run_id}").json()
        assert any(i["id"] == f"rescue-{body['id']}" and i["status"] == "pending" for i in plan["items"])
        listed = client.get(f"/incidents?run_id={run_id}").json()
        assert any(i["id"] == body["id"] and i["verification"] for i in listed)


def test_api_validation_and_unknown_run():
    client = TestClient(app)
    ok = {"text": "x", "lat": 18.5, "lng": 73.8}
    assert client.post("/incidents", json={**ok, "text": "   "}).status_code == 422
    assert client.post("/incidents", json={**ok, "lat": 120}).status_code == 422
    assert client.post("/incidents", json={**ok, "need_type": "nonsense"}).status_code == 422
    assert client.post("/incidents", json={**ok, "run_id": "nope"}).status_code == 404


def test_api_without_a_run_starts_one():
    run_store.active_run_id = None
    with TestClient(app) as client:
        res = client.post("/incidents", json={"text": "Flooded road", "lat": 18.52, "lng": 73.85, "need_type": "medical"})
        assert res.status_code == 201
        run_id = res.json()["run_id"]
        assert run_store.active_run_id == run_id
        for _ in range(100):
            run = run_store.get_run(run_id)
            if run.plan:
                break
            time.sleep(0.1)
        assert any(i.id.startswith("medical-manual-") for i in run.plan.items)
