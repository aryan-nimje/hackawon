"""Any-city loading: cache first, fetch + save on a miss, cache fallback on API failure, clear errors; no rescue route cap."""

import asyncio
import json
from datetime import datetime

import pytest
from fastapi.testclient import TestClient

import agents.route as route_agent
from config import get_settings
from main import app
from services import city as city_svc
from state import (
    HospitalAssignment, Incident, IncidentSource, NeedType, RescueAssignment, RunState, Urgency,
)

client = TestClient(app)


def _layers(slug="testville", name="Testville (Simulated)"):
    return {
        "city": {"name": name, "slug": slug, "center": [10.0, 20.0], "zoom": 12, "bbox": [9, 19, 11, 21]},
        "hospitals": [{"id": "h001", "name": "H", "lat": 10.0, "lng": 20.0, "beds": 10, "specialties": ["general"]}],
        "depots": [{"id": "w001", "name": "D", "lat": 10.0, "lng": 20.01, "stock": {"food_meals": 100, "vehicles": 2}}],
        "flood_zones": [{"id": "flood-1", "name": "Z", "reason": "r",
                         "ring": [[10.1, 20.1], [10.1, 20.2], [10.2, 20.2], [10.2, 20.1], [10.1, 20.1]]}],
        "bridges": [], "fire_stations": [], "police_stations": [],
    }


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    """Empty data dir + empty memory cache, live OSM on; the real Pune file is untouched."""
    monkeypatch.setattr(city_svc, "DATA_DIR", tmp_path)
    monkeypatch.setattr(city_svc, "_cache", {})
    monkeypatch.setenv("LIVE_OSM", "true")
    get_settings.cache_clear()
    yield tmp_path
    get_settings.cache_clear()


def _fetcher(monkeypatch, result=None, error=None):
    calls = []

    async def fake(name, *a, **k):
        calls.append(name)
        if error:
            raise error
        return result

    monkeypatch.setattr(city_svc, "fetch_city_layers", fake)
    return calls


def test_miss_fetches_saves_and_returns(data_dir, monkeypatch):
    calls = _fetcher(monkeypatch, _layers())
    out = asyncio.run(city_svc.get_layers("Testville"))
    assert out["city"]["slug"] == "testville" and calls == ["Testville"]
    saved = json.loads((data_dir / "layers_testville.json").read_text())
    assert saved["hospitals"][0]["id"] == "h001"
    assert not list(data_dir.glob("*.tmp"))


def test_second_request_uses_cache_not_the_apis(data_dir, monkeypatch):
    calls = _fetcher(monkeypatch, _layers())
    asyncio.run(city_svc.get_layers("Testville"))
    city_svc._cache.clear()  # force a read from disk, as after a restart
    out = asyncio.run(city_svc.get_layers("testville"))  # different case, same slug
    assert out["hospitals"] and len(calls) == 1


def test_existing_file_is_used_without_fetching(data_dir, monkeypatch):
    (data_dir / "layers_testville.json").write_text(json.dumps(_layers()))
    calls = _fetcher(monkeypatch, error=RuntimeError("must not be called"))
    assert asyncio.run(city_svc.get_layers("Testville"))["city"]["slug"] == "testville"
    assert calls == []


def test_api_failure_with_cache_returns_cache_on_refresh(data_dir, monkeypatch):
    (data_dir / "layers_testville.json").write_text(json.dumps(_layers()))
    _fetcher(monkeypatch, error=RuntimeError("overpass down"))
    out = asyncio.run(city_svc.get_layers("Testville", refresh=True))
    assert out["city"]["slug"] == "testville"


def test_api_failure_without_cache_is_a_clear_502(data_dir, monkeypatch):
    _fetcher(monkeypatch, error=RuntimeError("overpass down"))
    r = client.get("/layers?city=Testville")
    assert r.status_code == 502
    assert "Testville" in r.json()["detail"] and "no cached copy" in r.json()["detail"]
    assert not list(data_dir.glob("layers_*.json"))


def test_unknown_city_is_a_clear_404(data_dir, monkeypatch):
    _fetcher(monkeypatch, error=ValueError("City not found: Atlantis"))
    r = client.get("/layers?city=Atlantis")
    assert r.status_code == 404 and "not found" in r.json()["detail"]


def test_city_without_hospitals_is_not_saved(data_dir, monkeypatch):
    empty = {**_layers(), "hospitals": []}
    _fetcher(monkeypatch, empty)
    r = client.get("/layers?city=Testville")
    assert r.status_code == 404 and "no hospitals" in r.json()["detail"]
    assert not list(data_dir.glob("layers_*.json"))


def test_live_osm_off_and_no_cache_says_so(data_dir, monkeypatch):
    monkeypatch.setenv("LIVE_OSM", "false")
    get_settings.cache_clear()
    r = client.get("/layers?city=Testville")
    assert r.status_code == 404 and "LIVE_OSM=false" in r.json()["detail"]


def test_pune_cache_is_untouched_and_blank_city_means_default():
    from services.data_loader import DATA_DIR

    before = (DATA_DIR / "layers_pune.json").read_bytes()
    assert client.get("/layers?city=Pune").json()["city"]["slug"] == "pune"
    assert client.get("/layers?city=%20").json()["city"]["slug"] == "pune"
    assert (DATA_DIR / "layers_pune.json").read_bytes() == before


# ── Route Agent: every rescue assignment gets a route ─────────────────────
def test_route_agent_routes_all_rescue_assignments(monkeypatch):
    async def fake_leg(*a, **k):
        return 1.0, 2.0, [[0, 0], [1, 1]], False, False

    monkeypatch.setattr(route_agent, "_leg", fake_leg)
    run = RunState(id="r14")
    for n in range(14):
        run.incidents.append(Incident(
            id=f"i{n}", text="x", location="x", lat=18.52 + n * 0.001, lng=73.85, need_type=NeedType.RESCUE,
            urgency=Urgency.HIGH, source=IncidentSource.SIM, timestamp=datetime.utcnow(),
        ))
        run.rescue_queue.append(RescueAssignment(incident_id=f"i{n}", priority_score=50 - n, rank=n + 1, explanation="x"))
    out = asyncio.run(route_agent.run_route(run))
    assert len([r for r in out.routes if r.assignment_type == "rescue"]) == 14


# ── per-run city: agents plan against the run's city, not DEFAULT_CITY ─────
def test_city_context_switches_agent_data(data_dir):
    (data_dir / "layers_testville.json").write_text(json.dumps(_layers()))
    (data_dir / "layers_pune.json").write_text(json.dumps(_layers("pune", "Pune (Simulated)")))  # the default city
    from services.data_loader import load_hospitals, load_inventory

    with city_svc.city_context("testville"):
        assert [h["id"] for h in load_hospitals()] == ["h001"]
        assert load_inventory()[0]["name"] == "D"
        assert city_svc.get_city()["slug"] == "testville"
    assert city_svc.get_city()["slug"] != "testville"  # back to the default city outside the block


def test_scenario_start_unknown_city_is_a_clear_error(data_dir, monkeypatch):
    _fetcher(monkeypatch, error=ValueError("City not found: Atlantis"))
    r = client.post("/scenario/start", json={"city": "Atlantis"})
    assert r.status_code == 404 and "not found" in r.json()["detail"]


def test_run_for_another_city_uses_its_data_and_skips_pune_reports(data_dir, monkeypatch):
    (data_dir / "layers_testville.json").write_text(json.dumps(_layers()))
    from services.store import run_store
    from supervisor import start_scenario_replay

    run = asyncio.run(start_scenario_replay(replay_speed=10, city="testville"))
    assert run.city == "testville" and run.plan is not None
    ids = {i.id for i in run.incidents}
    assert not any(i.startswith(("r0", "news-")) for i in ids)  # Pune's reports and news are not replayed
    assert all(9 <= i.lat <= 11 and 19 <= i.lng <= 21 for i in run.incidents)  # incidents are in Testville
    assert run_store.active_run_id == run.id


def test_active_run_reports_its_city(data_dir):
    from services.store import run_store
    from supervisor import create_run

    run = create_run(city="testville")
    run_store.set_active(run.id)
    assert client.get("/runs/active").json()["city"] == "testville"
