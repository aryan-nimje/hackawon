"""API tests for the bus / sim / reports / replan endpoints."""

import pytest
from fastapi.testclient import TestClient

from main import app
from services.bus import bus
from services.reports import report_store
from services.store import run_store
from state import PlanItem, ResponsePlan, RunState
from supervisor import create_run

client = TestClient(app)


def _world(**over):
    w = {
        "vehicles": [{"id": "v1", "lat": 29.76, "lng": -95.37}],
        "disruptions": [],
        "affected_regions": [],
        "sim_incidents": [],
        "execution": [],
        "events": [],
        "beds": {},
        "urgency_override": {},
        "sim_time_s": 12.5,
        "ts": 1700000000000,
    }
    w.update(over)
    return w


def test_world_ok_and_stored():
    assert client.post("/sim/world", json=_world()).status_code == 200
    assert bus.latest_world["sim_time_s"] == 12.5


@pytest.mark.parametrize(
    "bad",
    [
        _world(vehicles=[{"id": "v", "lat": 123, "lng": 0}]),
        _world(vehicles=[{"id": "v", "lat": 10, "lng": 999}]),
        _world(vehicles=[{"id": "v", "lat": "x", "lng": 0}]),
        _world(sim_incidents=[{"id": "i", "lat": None, "lng": 1}]),
    ],
)
def test_world_bad_coords_422(bad):
    before = bus.latest_world
    assert client.post("/sim/world", json=bad).status_code == 422
    assert bus.latest_world is before


def test_world_missing_key_422():
    w = _world()
    del w["beds"]
    assert client.post("/sim/world", json=w).status_code == 422


def test_sim_event_validation():
    ok = {"kind": "route_blocked", "text": "x", "at": 3}
    assert client.post("/sim/event", json=ok).status_code == 200
    assert client.post("/sim/event", json={**ok, "kind": "nope"}).status_code == 422


def _run_with_plan():
    run = create_run()
    run.plan = ResponsePlan(
        run_id=run.id,
        items=[PlanItem(id="route-1", category="route", title="t", description="d", reasoning="r")],
    )
    run_store.update_run(run)
    return run


def test_replan_404s_and_ok():
    run = _run_with_plan()
    assert client.post("/plan/nope/replan", json={"item_id": "route-1", "action": "hold"}).status_code == 404
    assert client.post(f"/plan/{run.id}/replan", json={"item_id": "zzz", "action": "hold"}).status_code == 404
    assert client.post(f"/plan/{run.id}/replan", json={"item_id": "route-1", "action": "bad"}).status_code == 422
    assert client.post(f"/plan/{run.id}/replan", json={"item_id": "route-1", "action": "reroute"}).status_code == 200


def test_review_broadcasts_approved_once():
    run = _run_with_plan()
    q = bus.register()
    body = {"actions": [{"item_id": "route-1", "action": "approve"}]}
    client.post(f"/plan/{run.id}/review", json=body)
    client.post(f"/plan/{run.id}/review", json=body)
    msgs = []
    while not q.empty():
        msgs.append(q.get_nowait())
    bus.unregister(q)
    assert sum(m.startswith("event: plan.approved") for m in msgs) == 1
    assert sum(m.startswith("event: plan.updated") for m in msgs) == 2


REPORT = {
    "text": "Water is rising in our street",
    "lat": 29.76,
    "lng": -95.37,
    "accuracy_m": 20,
    "location_text": "Main St",
    "need_type": "rescue",
    "vulnerable": ["elderly"],
    "people_count": 3,
    "is_own_location": True,
    "reporter_lat": 29.76,
    "reporter_lng": -95.37,
    "contact": "555-0100",
    "website": "",
}


def test_report_lifecycle():
    r = client.post("/reports", json=REPORT)
    assert r.status_code == 200
    tok = r.json()["token"]
    assert r.json()["status"] == "received"
    s = client.get(f"/reports/{tok}").json()
    assert s["token"] == tok and s["history"][0]["status"] == "received"
    assert any(x["token"] == tok for x in client.get("/reports").json())
    assert client.get("/reports/unknown").status_code == 404


def test_report_validation_and_honeypot():
    assert client.post("/reports", json={**REPORT, "text": "short"}).status_code == 422
    assert client.post("/reports", json={**REPORT, "text": "x" * 501}).status_code == 422
    assert client.post("/reports", json={**REPORT, "lat": 91}).status_code == 422
    n = len(report_store.list())
    r = client.post("/reports", json={**REPORT, "website": "http://spam"})
    assert r.status_code == 200 and r.json()["status"] == "received"
    assert len(report_store.list()) == n
