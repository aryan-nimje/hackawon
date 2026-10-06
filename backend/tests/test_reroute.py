"""Route Agent re-routing: blocking rule, clean-detour search, and POST /plan/{id}/replan."""

import asyncio
import json

import pytest
from fastapi.testclient import TestClient

import services.routing as routing
from main import app
from services.bus import bus
from services.routing import (
    Hazard,
    _densify_points,
    blocking_hazards,
    find_clean_route,
    hazards_from_world,
    polygon_hazard,
)
from services.store import run_store
from state import PlanItem, ResponsePlan, RouteInfo
from supervisor import create_run

client = TestClient(app)

# A square hazard (~660 m wide) centred on (18.510, 73.850); ring in [lng, lat] order.
LAT0, LNG0, HALF = 18.510, 73.850, 0.003
RING = [[LNG0 - HALF, LAT0 - HALF], [LNG0 + HALF, LAT0 - HALF], [LNG0 + HALF, LAT0 + HALF], [LNG0 - HALF, LAT0 + HALF], [LNG0 - HALF, LAT0 - HALF]]
WEST, EAST = (LAT0, 73.830), (LAT0, 73.870)  # a straight road between them runs through the hazard
INSIDE = (LAT0, LNG0)


def _line(waypoints):
    """Fake OSRM: straight legs through the waypoints at 40 km/h."""
    pts = _densify_points(list(waypoints), 0.05)
    km = sum(routing.haversine_km(a[0], a[1], b[0], b[1]) for a, b in zip(waypoints, waypoints[1:]))
    return (km, km / 40 * 60, [[p[0], p[1]] for p in pts])


@pytest.fixture
def osrm(monkeypatch):
    calls = []

    async def fake(waypoints, alternatives=False, client=None):
        calls.append(list(waypoints))
        return [_line(waypoints)]

    monkeypatch.setattr(routing, "osrm_routes", fake)
    return calls


def test_blocking_rule():
    h = [polygon_hazard("flood", RING)]
    road = _line([WEST, EAST])[2]
    assert blocking_hazards(road, WEST, EAST, h)  # crosses, both endpoints outside -> blocked
    into = _line([WEST, INSIDE])[2]
    assert not blocking_hazards(into, WEST, INSIDE, h)  # rescue going into the hazard
    out = _line([INSIDE, EAST])[2]
    assert not blocking_hazards(out, INSIDE, EAST, h)  # patient leaving the hazard
    around = _line([WEST, (LAT0 + 0.01, LNG0), EAST])[2]
    assert not blocking_hazards(around, WEST, EAST, h)  # path never touches it


def test_thin_crossing_between_vertices_is_caught():
    # two far-apart vertices straddling the hazard: no vertex inside, but the segment crosses it
    assert blocking_hazards([list(WEST), list(EAST)], WEST, EAST, [polygon_hazard("flood", RING)])


async def test_get_route_flags_follow_the_rule(osrm):
    _, _, _, blocked = await routing.get_route(*WEST, *EAST, [RING])
    assert blocked
    _, _, _, into = await routing.get_route(*WEST, *INSIDE, [RING])
    assert not into
    _, _, _, leaving = await routing.get_route(*INSIDE, *EAST, [RING])
    assert not leaving


async def test_via_point_detour_is_clean_and_uses_osrm_time(osrm):
    res = await find_clean_route(WEST, EAST, [polygon_hazard("flood", RING, "Flood A")])
    assert res.route is not None and res.route.vias
    c = res.route
    assert not blocking_hazards(c.geometry, WEST, EAST, [polygon_hazard("flood", RING)])
    direct_km = routing.haversine_km(*WEST, *EAST)
    assert c.distance_km > direct_km  # real detour distance, not direct * a flat factor
    assert abs(c.duration_min - c.distance_km / 40 * 60) < 1e-6  # OSRM's own time for that path
    assert res.avoided == ["Flood A"]


async def test_clean_alternative_is_chosen_without_via_points(monkeypatch):
    north = _line([WEST, (LAT0 + 0.02, LNG0), EAST])

    async def fake(waypoints, alternatives=False, client=None):
        return [_line(waypoints), north] if alternatives else [_line(waypoints)]

    monkeypatch.setattr(routing, "osrm_routes", fake)
    res = await find_clean_route(WEST, EAST, [polygon_hazard("flood", RING)])
    assert res.route and not res.route.vias and res.requests == 1
    assert res.route.distance_km == pytest.approx(north[0])


async def test_fastest_of_several_clean_routes(monkeypatch):
    far = _line([WEST, (LAT0 + 0.05, LNG0), EAST])
    near = _line([WEST, (LAT0 + 0.02, LNG0), EAST])

    async def fake(waypoints, alternatives=False, client=None):
        return [_line(waypoints), far, near] if alternatives else [_line(waypoints)]

    monkeypatch.setattr(routing, "osrm_routes", fake)
    res = await find_clean_route(WEST, EAST, [polygon_hazard("flood", RING)])
    assert res.route.duration_min == pytest.approx(near[1])


async def test_no_clean_route(monkeypatch):
    blocked_line = _line([WEST, EAST])

    async def stubborn(waypoints, alternatives=False, client=None):
        return [blocked_line]  # OSRM only knows the road through the hazard

    monkeypatch.setattr(routing, "osrm_routes", stubborn)
    res = await find_clean_route(WEST, EAST, [polygon_hazard("flood", RING)])
    assert res.route is None and res.requests > 1


async def test_osrm_down_is_not_a_clean_route(monkeypatch):
    async def down(waypoints, alternatives=False, client=None):
        return []

    monkeypatch.setattr(routing, "osrm_routes", down)
    assert (await find_clean_route(WEST, EAST, [polygon_hazard("flood", RING)])).route is None


def test_hazards_from_world():
    world = {
        "affected_regions": [{"id": "r1", "name": "Flood", "ring": [[la, ln] for ln, la in RING]}],
        "disruptions": [
            {"id": "d1", "kind": "road_blocked", "latlng": [18.52, 73.86], "status": "active", "note": "blocked"},
            {"id": "d2", "kind": "bridge_collapsed", "geometry": [[18.53, 73.85], [18.533, 73.85]], "status": "active"},
            {"id": "d3", "kind": "vehicle_failed", "latlng": [18.5, 73.8], "status": "active"},
            {"id": "d4", "kind": "road_blocked", "latlng": [18.5, 73.8], "status": "cleared"},
        ],
    }
    hz = hazards_from_world(world)
    assert sorted(h.id for h in hz) == ["d1", "d2", "r1"]
    assert Hazard  # imported for type clarity


# ----------------------------------------------------------------------- POST /plan/{id}/replan


def _run(kind="rescue", target="inc1"):
    run = create_run()
    run.routes = [RouteInfo(assignment_id=target, assignment_type=kind, from_lat=18.49, from_lng=73.80,
                            to_lat=EAST[0], to_lng=EAST[1], distance_km=5, duration_min=9,
                            geometry=[[18.49, 73.80], list(EAST)], blocked_warning=False)]
    run.plan = ResponsePlan(run_id=run.id, routes=run.routes,
                            items=[PlanItem(id=f"{kind}-{target}", category=kind, title="t", description="d", reasoning="r")])
    run_store.update_run(run)
    return run


def _world(run, vehicle_at=WEST, regions=True, run_id="same", kind="rescue", target="inc1"):
    w = {
        "run_id": run.id if run_id == "same" else run_id,
        "vehicles": [{"id": "T1", "kind": kind, "lat": vehicle_at[0], "lng": vehicle_at[1], "target_id": target}],
        "disruptions": [], "affected_regions": [], "sim_incidents": [], "execution": [], "events": [],
        "beds": {}, "urgency_override": {}, "sim_time_s": 5.0, "ts": 1,
    }
    if regions:
        w["affected_regions"] = [{"id": "r1", "name": "Flood", "ring": [[la, ln] for ln, la in RING]}]
    bus.latest_world = w
    return w


def _drain(q):
    out = []
    while not q.empty():
        frame = q.get_nowait()
        name = frame.split("\n")[0].removeprefix("event: ")
        out.append((name, json.loads(frame.split("data: ", 1)[1])))
    return out


def test_replan_reroutes_from_current_position(osrm):
    run = _run()
    _world(run)
    q = bus.register()
    try:
        r = client.post(f"/plan/{run.id}/replan", json={"item_id": "rescue-inc1", "action": "reroute"})
    finally:
        bus.unregister(q)
    body = r.json()
    assert r.status_code == 200 and body["status"] == "rerouted" and body["ok"]
    route = body["route"]
    assert (route["from_lat"], route["from_lng"]) == WEST  # from where the vehicle is, not the depot
    assert (route["to_lat"], route["to_lng"]) == EAST
    assert route["geometry"][0] == pytest.approx(list(WEST)) and route["blocked_warning"] is False
    assert route["distance_km"] > 0 and route["duration_min"] > 0
    events = dict(_drain(q))
    assert events["plan.replan"]["route"] == route  # broadcast so the vehicle can follow it
    # the plan now carries the route the vehicle follows
    assert run_store.get_run(run.id).routes[0].from_lat == WEST[0]
    assert run_store.get_run(run.id).plan.routes[0].from_lat == WEST[0]


def test_replan_no_clean_detour(monkeypatch):
    run = _run()
    _world(run)
    straight = _line([WEST, EAST])

    async def stubborn(waypoints, alternatives=False, client=None):
        return [straight]

    monkeypatch.setattr(routing, "osrm_routes", stubborn)
    q = bus.register()
    try:
        body = client.post(f"/plan/{run.id}/replan", json={"item_id": "rescue-inc1", "action": "reroute"}).json()
    finally:
        bus.unregister(q)
    assert body["status"] == "no_clean_detour" and body["message"] == "no clean detour found"
    assert body.get("route") is None
    assert dict(_drain(q))["plan.replan"]["status"] == "no_clean_detour"
    assert run_store.get_run(run.id).routes[0].from_lat == 18.49  # plan untouched


def test_replan_into_hazard_is_not_blocked(osrm):
    run = _run()
    run.routes[0].to_lat, run.routes[0].to_lng = INSIDE  # rescue destination inside the flood
    _world(run)
    body = client.post(f"/plan/{run.id}/replan", json={"item_id": "rescue-inc1", "action": "reroute"}).json()
    assert body["status"] == "rerouted" and body["route"]["distance_km"] == pytest.approx(routing.haversine_km(*WEST, *INSIDE), rel=0.01)


def test_replan_hold_does_not_route(osrm):
    run = _run()
    _world(run)
    body = client.post(f"/plan/{run.id}/replan", json={"item_id": "rescue-inc1", "action": "hold"}).json()
    assert body["ok"] and "route" not in body and osrm == []


def test_replan_without_vehicle_or_for_other_run(osrm):
    run = _run()
    _world(run, run_id="some-other-run")
    assert client.post(f"/plan/{run.id}/replan", json={"item_id": "rescue-inc1", "action": "reroute"}).json()["status"] == "no_vehicle"
    _world(run, target="elsewhere")
    assert client.post(f"/plan/{run.id}/replan", json={"item_id": "rescue-inc1", "action": "reroute"}).json()["status"] == "no_vehicle"
    assert osrm == []


# ----------------------------------------------------------------------- vehicle stopped AT a roadblock (U-turn)

BLOCK = (LAT0, 73.850)  # a blocked road point in the middle of the straight road WEST -> EAST
STUCK = (LAT0, 73.850 - 0.17 / 105.5)  # the vehicle stopped ~170 m before it: inside the hazard's 180 m radius


def _roadblock():
    return Hazard(id="road-1", name="Road blocked", points=(BLOCK,))


def test_vehicle_stopped_at_roadblock_is_not_sent_through_it():
    road_on = _line([STUCK, EAST])[2]
    assert blocking_hazards(road_on, STUCK, EAST, [_roadblock()])  # driving on towards the blockage: still blocked
    road_back = _line([STUCK, WEST])[2]
    assert not blocking_hazards(road_back, STUCK, WEST, [_roadblock()])  # turning round / leaving it is fine


async def test_vehicle_stopped_at_roadblock_turns_round_and_detours(osrm):
    res = await find_clean_route(STUCK, EAST, [_roadblock()])
    assert res.route is not None and res.route.vias  # not the straight road through the blockage
    assert not blocking_hazards(res.route.geometry, STUCK, EAST, [_roadblock()])


async def test_osrm_requests_allow_u_turns(monkeypatch):
    seen = {}

    class FakeResp:
        status_code = 200

        def json(self):
            return {"code": "Ok", "routes": [{"distance": 1000, "duration": 60, "geometry": {"coordinates": [[73.8, 18.5], [73.81, 18.5]]}}]}

    class FakeClient:
        async def get(self, url, params=None):
            seen.update(params or {})
            return FakeResp()

    out = await routing.osrm_routes([WEST, EAST], client=FakeClient())
    assert out and seen.get("continue_straight") == "false"


async def test_moving_vehicle_route_is_constrained_to_its_heading():
    seen = {}

    class FakeResp:
        status_code = 200

        def json(self):
            return {"code": "Ok", "routes": [{"distance": 1000, "duration": 60, "geometry": {"coordinates": [[73.8, 18.5], [73.81, 18.5]]}}]}

    class FakeClient:
        async def get(self, url, params=None):
            seen.update(params or {})
            return FakeResp()

    await routing.osrm_routes([WEST, EAST], client=FakeClient(), heading=92.4)
    assert seen["bearings"] == "92,70;"
    seen.clear()
    await routing.osrm_routes([WEST, EAST], client=FakeClient())
    assert "bearings" not in seen


async def test_reroute_prefers_forward_detour_and_falls_back_to_turning_round(monkeypatch):
    calls = []

    async def fake(waypoints, alternatives=False, client=None, heading=None):
        calls.append(heading)
        if heading is not None:  # no forward-only route exists
            return []
        return [(1.0, 2.0, [list(WEST), list(EAST)])]

    monkeypatch.setattr(routing, "osrm_routes", fake)
    from agents.route import run_reroute

    out = await run_reroute("x", "rescue", WEST, EAST, [], heading=90.0)
    assert out.status == "rerouted" and "turn round" in out.route.explanation
    assert calls[0] == 90.0 and calls[-1] is None
