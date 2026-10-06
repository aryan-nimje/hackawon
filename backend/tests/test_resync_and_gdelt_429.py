"""Memory follows the database (resync) and GDELT 429 handling."""

import asyncio
from datetime import datetime

import httpx
import pytest

from services import persistence, sim_reset
from services.reports import ReportStore
from services.resync import resync_from_db
from services.signals import common, gdelt, ingest
from services.signals.store import SignalStore
from services.store import RunStore
from state import Incident, IncidentSource, NeedType, RunState, Urgency


def _inc(i):
    return Incident(id=i, text="Water rising", location="x", lat=18.5, lng=73.8, need_type=NeedType.RESCUE,
                    urgency=Urgency.HIGH, source=IncidentSource.CITIZEN, timestamp=datetime(2026, 10, 2, 10, 0))


def _db(monkeypatch, runs=(), active=None, incidents=()):
    monkeypatch.setattr(persistence, "load_runs_strict", lambda: ([r.model_dump(mode="json") for r in runs], active))
    monkeypatch.setattr(persistence, "load_incidents_strict", lambda: [i.model_dump(mode="json") for i in incidents])


def test_runs_and_incidents_deleted_from_the_database_leave_memory(monkeypatch):
    store = RunStore()
    gone, kept = RunState(id="gone"), RunState(id="kept")
    store._runs = {"gone": gone, "kept": kept}
    store._all_incidents = {"i-gone": _inc("i-gone"), "i-kept": _inc("i-kept")}
    store.active_run_id = "gone"
    _db(monkeypatch, runs=[kept], active="kept", incidents=[_inc("i-kept")])

    out = store.sync_from_db()

    assert out == {"runs_dropped": 1, "runs_added": 0, "incidents_dropped": 1, "dropped_run_ids": ["gone"]}
    assert list(store._runs) == ["kept"] and list(store._all_incidents) == ["i-kept"]
    assert store.active_run_id == "kept"
    assert store._runs["kept"] is kept  # a run present in both is the same object: a pipeline may be holding it
    store.update_run(gone)  # a pipeline still working on the dropped run cannot bring it back
    assert "gone" not in store._runs


def test_rows_only_in_the_database_are_picked_up(monkeypatch):
    store = RunStore()
    _db(monkeypatch, runs=[RunState(id="db-run")], active="db-run", incidents=[_inc("db-inc")])
    assert store.sync_from_db()["runs_added"] == 1
    assert store.get_active_run().id == "db-run" and "db-inc" in store._all_incidents


def test_an_unreachable_database_never_empties_memory(monkeypatch):
    store = RunStore()
    store._runs = {"a": RunState(id="a")}
    monkeypatch.setattr(persistence, "load_runs_strict", lambda: None)
    monkeypatch.setattr(persistence, "load_incidents_strict", lambda: [])
    assert store.sync_from_db() is None
    assert "a" in store._runs


def test_reports_and_signals_follow_the_database(monkeypatch):
    reports = ReportStore()
    reports._reports = {"old": {"token": "old", "created_at": "x", "submission": {}, "run_id": None}}
    monkeypatch.setattr(persistence, "load_submissions_strict", lambda limit: [])
    assert reports.sync_from_db() == 1 and reports._reports == {}
    monkeypatch.setattr(persistence, "load_submissions_strict", lambda limit: None)
    reports._reports = {"keep": {"token": "keep"}}
    assert reports.sync_from_db() is None and "keep" in reports._reports

    signals = SignalStore()
    monkeypatch.setattr(persistence, "load_signals_strict", lambda: [])
    signals._signals = {"s": object()}
    assert signals.sync_from_db() == 1 and signals._signals == {}


async def test_reset_simulation_first_agrees_with_the_database(monkeypatch):
    from services.store import run_store
    from services.reports import report_store

    ghost = RunState(id="ghost-real-run")  # a REAL run (not simulated) that was deleted from the database by hand
    run_store._runs[ghost.id] = ghost
    run_store.active_run_id = ghost.id
    report_store._reports["t"] = {"token": "t", "created_at": "x", "submission": {}, "run_id": None}
    _db(monkeypatch)
    monkeypatch.setattr(persistence, "load_submissions_strict", lambda limit: [])
    monkeypatch.setattr(persistence, "load_signals_strict", lambda: [])
    try:
        out = await sim_reset.reset_simulation()
        assert out["db_sync"]["synced"] is True and out["db_sync"]["runs_dropped"] >= 1
        assert run_store.get_run(ghost.id) is None and run_store.active_run_id is None
        assert "t" not in report_store._reports
    finally:
        run_store._runs.pop(ghost.id, None)
        report_store._reports.pop("t", None)


def test_resync_reports_not_synced_without_a_database():
    assert resync_from_db() == {"synced": False}  # DATABASE_URL is unset in tests


# ───────────────────────────── GDELT 429 ─────────────────────────────
PUNE = common.CityRef(slug="pune", label="Pune", lat=18.5, lng=73.8, bbox=[18.3, 73.6, 18.7, 74.0])
OTHER = common.CityRef(slug="x", label="Xville", lat=1.0, lng=2.0, bbox=[0, 1, 2, 3])


def _client(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_429_is_retried_waiting_what_gdelt_asked(monkeypatch):
    sleeps = []

    async def fake_sleep(s):
        sleeps.append(s)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    answers = [httpx.Response(429, headers={"Retry-After": "30"}), httpx.Response(200, json={"articles": []})]
    async with _client(lambda r: answers.pop(0)) as c:
        res = await gdelt.fetch_gdelt([PUNE], client=c)
    assert res.ok and not res.rate_limited and 30.0 in sleeps


async def test_persistent_429_stops_asking_and_is_reported(monkeypatch):
    async def fake_sleep(s):
        pass

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(429)

    async with _client(handler) as c:
        res = await gdelt.fetch_gdelt([PUNE, OTHER], client=c)
    assert res.rate_limited and not res.ok
    assert len(calls) == 3  # first try + GDELT_RETRIES, and the second city was never asked


async def test_after_a_429_gdelt_is_left_alone_for_the_cooldown(monkeypatch):
    async def fake_sleep(s):
        pass

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(429)

    async with _client(handler) as c:
        first = await ingest.refresh_gdelt(client=c)
        n = len(calls)
        second = await ingest.refresh_gdelt(client=c)
    assert first["rate_limited"] is True
    assert second["rate_limited"] is True and second["cooldown_seconds"] > 0 and len(calls) == n


def test_the_first_poll_after_startup_waits(monkeypatch):
    monkeypatch.setenv("GDELT_STARTUP_DELAY_SECONDS", "20")
    from config import get_settings
    get_settings.cache_clear()
    assert ingest._gdelt_first_delay() == 20.0


# ───────────────────────────── background re-sync, seed data ─────────────────────────────
def test_a_run_too_young_for_the_database_is_kept_by_the_background_check(monkeypatch):
    store = RunStore()
    store._runs = {"new": RunState(id="new")}  # created just now
    _db(monkeypatch)
    assert store.sync_from_db(grace_s=30)["runs_dropped"] == 0 and "new" in store._runs
    assert store.sync_from_db(grace_s=0)["runs_dropped"] == 1  # an explicit reset / start does not wait


def test_background_resync_tells_clients_when_a_run_vanished(monkeypatch):
    from services.bus import bus
    from services.store import run_store

    old = RunState(id="vanished", created_at=datetime(2026, 1, 1))
    run_store._runs[old.id] = old
    run_store.active_run_id = old.id
    _db(monkeypatch)
    monkeypatch.setattr(persistence, "load_submissions_strict", lambda limit: [])
    monkeypatch.setattr(persistence, "load_signals_strict", lambda: [])
    q = bus.register()
    try:
        out = resync_from_db(publish=True, grace_s=30)
        msgs = []
        while not q.empty():
            msgs.append(q.get_nowait())
    finally:
        bus.unregister(q)
        run_store._runs.pop(old.id, None)
    assert out["runs_dropped"] == 1
    assert any(m.startswith("event: sim.reset") and "vanished" in m for m in msgs)


def test_demo_seed_data_is_off_by_default(monkeypatch):
    from config import Settings
    monkeypatch.delenv("SEED_REPORTS", raising=False)
    assert Settings(_env_file=None).seed_reports is False
