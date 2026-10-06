"""Simulation evidence controls (POST /sim/evidence), the contradiction scoring path, and POST /sim/reset."""

import random
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from agents.verification import score_detail
from main import app
from services import corroboration as cor
from services import sim_evidence
from services.bus import bus
from services.signals.models import Signal, SignalKind, SignalSource
from services.signals.store import signal_store
from services.store import run_store
from state import (
    Incident,
    IncidentSource,
    NeedType,
    PlanItem,
    RescueAssignment,
    ResponsePlan,
    Urgency,
    VerificationResult,
)
from supervisor import create_run

client = TestClient(app)
LAT, LNG = 18.5130, 73.8455


def inc(id_="s1", text="Water rising fast on my street, flooding near Deccan", source=IncidentSource.SIM, ts=None, **kw):
    return Incident(id=id_, text=text, location=kw.pop("location", "Deccan Gymkhana, Pune"), lat=LAT, lng=LNG,
                    need_type=NeedType.SHELTER, urgency=Urgency.MEDIUM, source=source,
                    timestamp=ts or datetime.utcnow(), raw_metadata=kw)


def _run_with(*incidents, simulated=False):
    run = create_run(simulated=simulated)
    run.incidents.extend(incidents)
    for i in incidents:
        run_store.upsert_incident(i)
    run_store.update_run(run)
    return run


# --------------------------------------------------------------------------------------- the signals themselves
@pytest.mark.parametrize("kind", [*sim_evidence.SUPPORT_KINDS, *sim_evidence.CONTRADICT_KINDS])
def test_every_kind_is_an_ordinary_signal_built_from_the_incident(kind):
    i = inc()
    sig = sim_evidence.build_signal(i, kind)
    assert isinstance(sig, Signal) and sig.id == f"sim:{i.id}:{kind}"
    assert sig.metadata["simulated"] is True and sig.metadata["stance"] == sim_evidence.stance_of(kind)
    assert sig.hazards == ["flood"]  # the incident's own hazard
    assert (sig.lat, sig.lng) == (i.lat, i.lng)  # the incident's place
    assert sig.issued_at is not None and abs((sig.issued_at - datetime.utcnow()).total_seconds()) < 5  # now
    assert sig.weight >= cor.config().evidence_min_signal_weight  # strong enough to be used, via the normal formula
    assert {"news": SignalKind.NEWS, "official_alert": SignalKind.OFFICIAL_ALERT, "weather": SignalKind.WEATHER,
            "all_clear": SignalKind.OFFICIAL_ALERT, "normal_conditions": SignalKind.WEATHER}[kind] == sig.kind


def test_incident_without_a_hazard_word_takes_the_hazard_of_the_nearest_incident():
    quiet = inc("q1", text="My neighbour needs insulin, trapped on the 2nd floor")
    with pytest.raises(sim_evidence.NoHazard):  # nothing in the run names a hazard either
        sim_evidence.build_signal(quiet, "news")
    flood = inc("f1")  # same event: names flooding
    sig = sim_evidence.build_signal(quiet, "official_alert", context=[quiet, flood])
    assert sig.hazards == ["flood"]
    # its text names no hazard, yet the evidence addressed to it is scored (supporting and contradicting paths)
    base = score_detail(quiet, [quiet], []).score
    assert score_detail(quiet, [quiet], [sig]).score > base
    clear = sim_evidence.build_signal(quiet, "all_clear", context=[quiet, flood])
    assert score_detail(quiet, [quiet], [clear]).score < base


def test_evidence_addressed_to_one_incident_does_not_touch_another():
    a, b = inc("a2", text="Trapped, needs help"), inc("b2", text="Trapped, needs help too")
    sig = sim_evidence.build_signal(a, "official_alert", context=[a, inc("f2")])
    assert score_detail(b, [b], [sig]).score == score_detail(b, [b], []).score


def test_real_signals_still_need_a_hazard_word_in_the_incident_text():
    quiet = inc("q3", text="Trapped on the roof, send help")
    real = Signal(id="sachet:z", source=SignalSource.SACHET, kind=SignalKind.OFFICIAL_ALERT, title="Flood warning",
                  hazards=["flood"], weight=0.6, trust=0.9, bbox=[LAT - 0.01, LNG - 0.01, LAT + 0.01, LNG + 0.01],
                  issued_at=datetime.utcnow() - timedelta(hours=1), expires_at=datetime.utcnow() + timedelta(hours=5))
    assert cor.find_support(quiet, [real]) == []  # unchanged real-evidence behaviour


def test_signal_window_covers_an_older_incident():
    old = inc(ts=datetime.utcnow() - timedelta(days=4))  # replayed scenario reports are days old
    sig = sim_evidence.build_signal(old, "official_alert")
    assert cor.find_support(old, [sig])  # time overlap holds


# --------------------------------------------------------------------------------------- scoring paths
def test_backing_evidence_uses_the_supporting_path():
    i = inc()
    base = score_detail(i, [i], []).score
    for kind in sim_evidence.SUPPORT_KINDS:
        sig = sim_evidence.build_signal(i, kind)
        d = score_detail(i, [i], [sig])
        assert d.score > base and d.supported_by == [sig.id] and d.contradicted_by == []


def test_contradicting_evidence_uses_the_contradiction_path_and_never_counts_as_support():
    i = inc()
    base = score_detail(i, [i], []).score
    for kind in sim_evidence.CONTRADICT_KINDS:
        sig = sim_evidence.build_signal(i, kind)
        assert cor.find_support(i, [sig]) == []  # an all-clear is not support, even though its hazard matches
        d = score_detail(i, [i], [sig])
        assert d.score < base and d.contradicted_by == [sig.id] and d.supported_by == []


def test_support_and_contradiction_net_out_and_penalty_is_capped():
    i = inc()
    news = sim_evidence.build_signal(i, "news")
    clear = sim_evidence.build_signal(i, "all_clear")
    normal = sim_evidence.build_signal(i, "normal_conditions")
    both = score_detail(i, [i], [news, clear]).score
    assert score_detail(i, [i], [clear]).score < both < score_detail(i, [i], [news]).score
    cfg = cor.config()
    assert cor.contradiction_penalty(cor.find_contradiction(i, [clear, normal])) <= cfg.contradiction_max_penalty


def test_contradiction_for_another_hazard_or_place_is_ignored():
    i = inc()
    fire_clear = sim_evidence.build_signal(inc(text="Building fire on the 3rd floor"), "all_clear")
    far = sim_evidence.build_signal(i, "all_clear").model_copy(update={
        "lat": 19.5, "lng": 74.5, "bbox": [19.49, 74.49, 19.51, 74.51], "circles": [[19.5, 74.5, 2.0]]})
    assert score_detail(i, [i], [fire_clear, far]).score == score_detail(i, [i], []).score


def test_feed_incidents_and_flagged_reports_are_not_moved():
    clear = lambda i: sim_evidence.build_signal(i, "all_clear")  # noqa: E731
    weather = inc("w1", source=IncidentSource.WEATHER)
    assert score_detail(weather, [weather], [clear(inc("x"))]).score == 0.95
    junk = inc("j1", suspicious=True)
    assert score_detail(junk, [junk], [clear(junk)]).contradicted_by == []


# --------------------------------------------------------------------------------------- randomise
def test_randomise_draws_supporting_or_contradicting_independently_each_time():
    r = random.Random(7)
    stances = {sim_evidence.stance_of(sim_evidence.resolve_action("random", r)) for _ in range(60)}
    assert stances == {"supports", "contradicts"}
    kinds = {sim_evidence.resolve_action("random", random.Random(s)) for s in range(60)}
    assert kinds == {*sim_evidence.SUPPORT_KINDS, *sim_evidence.CONTRADICT_KINDS}


def test_randomise_is_per_event_not_one_choice_for_the_whole_response(monkeypatch):
    a, b = inc("a1"), inc("b1", text="Flooding in the lane behind the market")
    run = _run_with(a, b)
    draws = iter([0.9, 0.1])  # first event: contradicting pool; second event: supporting pool
    monkeypatch.setattr(sim_evidence.rng, "random", lambda: next(draws))
    first = client.post("/sim/evidence", json={"run_id": run.id, "incident_id": "a1", "action": "random"}).json()
    second = client.post("/sim/evidence", json={"run_id": run.id, "incident_id": "b1", "action": "random"}).json()
    assert first["stance"] == "contradicts" and second["stance"] == "supports"
    assert signal_store.get(first["signal_id"]).metadata["stance"] == "contradicts"
    assert signal_store.get(second["signal_id"]).metadata["stance"] == "supports"


# --------------------------------------------------------------------------------------- the endpoint
def test_evidence_endpoint_changes_credibility_through_the_pipeline():
    i = inc("e1")
    run = _run_with(i)
    url = "/sim/evidence"
    body = lambda action: {"run_id": run.id, "incident_id": "e1", "action": action}  # noqa: E731

    up = client.post(url, json=body("official_alert")).json()
    assert up["ok"] and up["stance"] == "supports" and up["signal_id"] in signal_store._signals
    assert up["verification"]["supported_by"] == [up["signal_id"]]
    listed = client.get(f"/incidents?run_id={run.id}").json()[0]
    assert listed["verification"]["credibility"] == up["credibility"]  # what the dashboards read

    down = client.post(url, json=body("all_clear")).json()
    assert down["stance"] == "contradicts" and down["credibility"] < up["credibility"]
    assert down["verification"]["contradicted_by"] == [down["signal_id"]]
    assert len([v for v in run_store.get_run(run.id).verifications if v.incident_id == "e1"]) == 1  # replaced, not stacked


def test_evidence_endpoint_errors():
    run = _run_with(inc("e2"), inc("e3", text="Man trapped, needs help"), inc("e4", source=IncidentSource.WEATHER))
    post = lambda rid, iid: client.post("/sim/evidence", json={"run_id": rid, "incident_id": iid, "action": "news"})  # noqa: E731
    assert post("nope", "e2").status_code == 404
    assert post(run.id, "missing").status_code == 404
    assert post(run.id, "e3").status_code == 200  # names no hazard itself: takes the flood of the incident next to it
    alone = _run_with(inc("e5", text="Man trapped, needs help"))
    assert post(alone.id, "e5").status_code == 422  # no hazard anywhere in the run
    assert post(run.id, "e4").status_code == 422  # a weather incident is evidence itself
    assert client.post("/sim/evidence", json={"run_id": run.id, "incident_id": "e2", "action": "bogus"}).status_code == 422


# --------------------------------------------------------------------------------------- reset
def test_reset_removes_only_simulation_state_and_restores_the_real_run():
    real_inc = inc("real1", source=IncidentSource.CITIZEN)
    real = _run_with(real_inc)
    real_sig = Signal(id="sachet:real", source=SignalSource.SACHET, kind=SignalKind.OFFICIAL_ALERT, title="Heavy rain warning",
                      hazards=["rain"], weight=0.6, trust=0.9)
    signal_store.upsert_many([real_sig])

    sim_inc = inc("sim1")
    sim = _run_with(sim_inc, simulated=True)
    sim_ev = sim_evidence.build_signal(sim_inc, "news")
    signal_store.upsert_many([sim_ev])
    run_store.set_active(sim.id)
    bus.latest_world = {"run_id": sim.id}

    # a simulation-added incident that was put into the REAL run, with the plan items made for it
    added = inc("manual-x1")
    real.incidents.append(added)
    real.rescue_queue.append(RescueAssignment(incident_id="manual-x1", priority_score=1.0, rank=1, explanation="x"))
    real.plan = ResponsePlan(run_id=real.id, items=[
        PlanItem(id="rescue-manual-x1", category="rescue", title="t", description="d", reasoning="r"),
        PlanItem(id="rescue-real1", category="rescue", title="t", description="d", reasoning="r")])
    # a real incident whose credibility had been moved by simulated evidence
    real.verifications.append(VerificationResult(incident_id="real1", credibility=0.9, reasons=["x"], supported_by=[sim_ev.id]))
    run_store.update_run(real)

    out = client.post("/sim/reset").json()

    assert out["ok"] and out["removed_runs"] >= 1 and out["active_run_id"] == real.id
    assert run_store.get_run(sim.id) is None and run_store.is_removed(sim.id)  # simulation run gone
    assert run_store.get_run(real.id) is not None and run_store.active_run_id == real.id  # real run is active again
    assert signal_store.get(sim_ev.id) is None and signal_store.get(real_sig.id) is not None  # only the simulated signal went
    run = run_store.get_run(real.id)
    assert [i.id for i in run.incidents] == ["real1"]  # the simulation incident left the real run ...
    assert [i.id for i in run.plan.items] == ["rescue-real1"] and run.rescue_queue == []  # ... with its plan items
    v = next(v for v in run.verifications if v.incident_id == "real1")
    assert v.supported_by == [] and v.credibility < 0.9  # scored again without the simulated evidence
    assert bus.latest_world is None


def test_late_world_from_a_removed_run_is_ignored():
    sim = _run_with(inc("w9"), simulated=True)
    client.post("/sim/reset")
    world = {"vehicles": [], "disruptions": [], "affected_regions": [], "sim_incidents": [], "execution": [], "events": [],
             "beds": {}, "urgency_override": {}, "sim_time_s": 1.0, "ts": 1, "run_id": sim.id}
    assert client.post("/sim/world", json=world).json().get("ignored") is True
    assert bus.latest_world is None
    assert run_store.get_run(sim.id) is None  # and nothing brought the run back


def test_a_removed_run_cannot_be_written_back():
    sim = _run_with(inc("w8"), simulated=True)
    client.post("/sim/reset")
    run_store.update_run(sim)  # what a still-running pipeline would do
    assert run_store.get_run(sim.id) is None


def test_scenario_start_request_carries_the_simulation_flag():
    from state import ScenarioStartRequest

    assert ScenarioStartRequest().simulation is False
    assert ScenarioStartRequest(simulation=True).simulation is True
    assert create_run(simulated=True).simulated is True and create_run().simulated is False
