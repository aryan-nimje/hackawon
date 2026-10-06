"""Persistence + citizen DB reader. Uses SQLite files as stand-ins; set TEST_POSTGRES_URL to run on Postgres."""

import os
from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine, text

import db
from config import get_settings
from services import citizen_db, persistence
from services.reports import ReportStore
from services.store import RunStore
from state import Incident, IncidentSource, NeedType, RunState, Urgency


@pytest.fixture
def database(tmp_path, monkeypatch):
    url = os.environ.get("TEST_POSTGRES_URL") or f"sqlite:///{tmp_path / 'relief.db'}"
    monkeypatch.setenv("DATABASE_URL", url)
    get_settings.cache_clear()
    db.reset_engine()
    assert db.init_db()
    yield url
    from sqlmodel import SQLModel

    if os.environ.get("TEST_POSTGRES_URL"):
        SQLModel.metadata.drop_all(db.get_engine())
    db.reset_engine()


def _incident(i="inc-1"):
    return Incident(id=i, text="Water rising", location="Deccan", lat=18.51, lng=73.84, need_type=NeedType.RESCUE,
                    urgency=Urgency.HIGH, source=IncidentSource.CITIZEN, timestamp=datetime(2026, 10, 2, 10, 0))


def test_normalize_url():
    assert db.normalize_url("postgres://u:p@h/d") == "postgresql+psycopg://u:p@h/d"
    assert db.normalize_url("postgresql://u:p@h/d") == "postgresql+psycopg://u:p@h/d"
    assert db.normalize_url("sqlite:///x.db") == "sqlite:///x.db"


@pytest.mark.parametrize("raw", [
    '"postgresql://u:p@h.render.com/db?sslmode=require"',
    "  postgresql://u:p@h.render.com/db?sslmode=require  # my db",
    "DATABASE_URL=postgresql://u:p@h.render.com/db?sslmode=require",
    "psql postgresql://u:p@h.render.com/db?sslmode=require",
    "postgres://u:p@h.render.com/db?sslmode=require",
])
def test_pasted_url_mistakes_are_forgiven(raw):
    assert db.normalize_url(raw) == "postgresql+psycopg://u:p@h.render.com/db?sslmode=require"
    db.build_engine(raw).dispose()


@pytest.mark.parametrize("raw", ["postgresql://USER:<PASSWORD>@[HOST]/db", "PGPASSWORD=secret psql -h host -U u db", "just some text"])
def test_bad_url_gives_a_clear_error_without_leaking_the_password(raw):
    with pytest.raises(RuntimeError) as e:
        db.build_engine(raw)
    msg = str(e.value)
    assert "postgresql://USER:PASSWORD@HOST" in msg and "password hidden" in msg


def test_error_message_masks_password():
    with pytest.raises(RuntimeError) as e:
        db.build_engine("postgresql://u:hunter2@[bad host/db")
    assert "hunter2" not in str(e.value)


def test_disabled_without_database_url(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    get_settings.cache_clear()
    db.reset_engine()
    assert db.get_engine() is None and not persistence.enabled() and db.ping() is None
    store = RunStore()
    store.create_run(RunState(id="r0"))  # still works, in memory
    assert store.get_run("r0")


def test_runs_incidents_and_active_run_survive_restart(database):
    store = RunStore()
    run = store.create_run(RunState(id="run-a", city="pune"))
    store.upsert_incident(_incident())
    run.incidents.append(_incident())
    store.update_run(run)
    store.create_run(RunState(id="run-b"))
    store.set_active("run-a")

    restarted = RunStore()  # a brand-new process
    assert restarted.load_from_db() == 2
    assert restarted.active_run_id == "run-a"
    assert restarted.get_run("run-a").city == "pune"
    assert [i.id for i in restarted.get_run("run-a").incidents] == ["inc-1"]
    assert [i.id for i in restarted.list_incidents()] == ["inc-1"]


def test_update_run_overwrites_not_duplicates(database):
    store = RunStore()
    run = store.create_run(RunState(id="run-a"))
    run.revision_count = 3
    store.update_run(run)
    assert RunStore().load_from_db() == 1
    again = RunStore()
    again.load_from_db()
    assert again.get_run("run-a").revision_count == 3


def test_report_submissions_survive_restart(database):
    store = ReportStore()
    rec = store.add({"text": "help me please", "lat": 18.5, "lng": 73.8}, run_id="run-a")
    fresh = ReportStore()
    assert fresh.load_from_db() == 1
    assert fresh.get(rec["token"])["run_id"] == "run-a"
    assert fresh.status_view(fresh.get(rec["token"]))["status"] == "received"


# -- citizen app database (read-only): schema taken from the deployed server.js ------------------------
import json


@pytest.fixture
def citizen(tmp_path, monkeypatch):
    path = tmp_path / "citizen.db"
    eng = create_engine(f"sqlite:///{path}")
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    meta = json.dumps({"people_count": 4, "vulnerable": ["elderly", "limited_mobility"], "here": False, "phone": "+1 555 0100"})
    with eng.begin() as c:
        c.execute(text('CREATE TABLE reports (id INTEGER PRIMARY KEY, text TEXT, location TEXT, lat REAL, lng REAL,'
                       ' need_type TEXT, urgency TEXT, source TEXT, "timestamp" TIMESTAMP, metadata TEXT)'))
        ins = text("INSERT INTO reports VALUES (:i,:t,'Deccan',:lat,:lng,:n,:u,'citizen',:ts,:m)")
        c.execute(ins, dict(i=1, t="Flooded street, family stuck", lat=18.51, lng=73.84, n="food_water", u="moderate", ts=now, m=meta))
        c.execute(ins, dict(i=2, t="no coordinates here", lat=None, lng=None, n="other", u="low", ts=now, m="{}"))
        c.execute(ins, dict(i=3, t="very old report", lat=18.5, lng=73.8, n="rescue", u="critical", ts="2020-01-01 00:00:00", m="{}"))
    monkeypatch.setenv("CITIZEN_DATABASE_URL", f"sqlite:///{path}")
    get_settings.cache_clear()
    citizen_db.reset()
    yield eng
    citizen_db.reset()


def test_citizen_reports_are_mapped_and_filtered(citizen):
    reports = citizen_db.fetch_recent()
    assert [r["id"] for r in reports] == ["cr-1"]  # bad coordinates and out-of-window rows dropped
    r = reports[0]
    assert (r["lat"], r["lng"], r["source"]) == (18.51, 73.84, "citizen")
    assert r["need_type"] == "supplies" and r["urgency"] == "medium"  # food_water / moderate translated
    assert r["metadata"]["vulnerable"] == ["elderly", "limited_mobility"]
    assert r["metadata"]["people_count"] == 4 and r["metadata"]["reported_for_someone_else"] is True
    assert "phone" not in json.dumps(r) and "555" not in json.dumps(r)  # reporter's number never leaves the citizen DB
    assert citizen_db.ping() is True


def test_citizen_db_is_only_read(citizen):
    citizen_db.fetch_recent()
    with citizen.connect() as c:
        assert c.execute(text("SELECT COUNT(*) FROM reports")).scalar() == 3


async def test_detection_uses_citizen_db(citizen, monkeypatch):
    monkeypatch.setenv("SEED_REPORTS", "false")
    get_settings.cache_clear()
    from agents.detection import run_detection

    out = await run_detection(RunState(id="r"))
    cit = [i for i in out.incidents if i.source == IncidentSource.CITIZEN]
    assert [i.id for i in cit] == ["cr-1"]
    assert cit[0].need_type == NeedType.SUPPLIES and cit[0].urgency == Urgency.MEDIUM


async def test_sync_adds_only_new_reports_to_the_active_run(citizen, monkeypatch):
    import supervisor
    from services import citizen_sync

    citizen_sync.reset_sync()
    planned = []
    monkeypatch.setattr(supervisor, "schedule_incident_planning", lambda rid, iid: planned.append((rid, iid)))
    store = RunStore()
    monkeypatch.setattr(citizen_sync, "run_store", store)
    assert await citizen_sync.sync_once() == 0  # no active run: nothing to do

    run = store.create_run(RunState(id="live"))
    store.set_active("live")
    assert await citizen_sync.sync_once() == 1
    assert [i.id for i in store.get_run("live").incidents] == ["cr-1"]
    assert planned == [("live", "cr-1")]
    assert await citizen_sync.sync_once() == 0  # already in the run

    with citizen.begin() as c:  # a citizen submits while the run is live
        c.execute(text("INSERT INTO reports VALUES (9,'Roof collapsed, need rescue now','Kothrud',18.5,73.8,'rescue','critical','citizen',:ts,'{}')"),
                  {"ts": datetime.now(timezone.utc).replace(tzinfo=None)})
    assert await citizen_sync.sync_once() == 1
    assert [i.id for i in store.get_run("live").incidents] == ["cr-1", "cr-9"]


def test_citizen_schema_mismatch_returns_empty_and_does_not_raise(tmp_path, monkeypatch):
    path = tmp_path / "weird.db"
    with create_engine(f"sqlite:///{path}").begin() as c:
        c.execute(text("CREATE TABLE reports (id INTEGER, foo TEXT)"))
    monkeypatch.setenv("CITIZEN_DATABASE_URL", f"sqlite:///{path}")
    get_settings.cache_clear()
    citizen_db.reset()
    assert citizen_db.fetch_recent() == []
    citizen_db.reset()


# -- ONE database: citizens write `reports`, the backend reads it and keeps its own tables beside it -----
@pytest.fixture
def one_db(tmp_path, monkeypatch):
    url = f"sqlite:///{tmp_path / 'shared.db'}"
    eng = create_engine(url)
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    meta = json.dumps({"people_count": 3, "vulnerable": ["limited_mobility"], "here": True, "phone": "+1 555 0199"})
    with eng.begin() as c:  # the citizen app's table, created by the citizen app
        c.execute(text('CREATE TABLE reports (id TEXT PRIMARY KEY, text TEXT, location TEXT, lat REAL, lng REAL, need_type TEXT,'
                       ' urgency TEXT, source TEXT, "timestamp" TIMESTAMP, metadata TEXT, client_token TEXT UNIQUE)'))
        c.execute(text("INSERT INTO reports VALUES ('11111111-aaaa','Trapped on 2nd floor','Deccan',18.51,73.84,'rescue','critical','citizen',:t,:m,NULL)"),
                  {"t": now, "m": meta})
    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.delenv("CITIZEN_DATABASE_URL", raising=False)
    get_settings.cache_clear()
    db.reset_engine()
    citizen_db.reset()
    yield eng
    citizen_db.reset()
    db.reset_engine()


def test_one_database_serves_both_reports_and_backend_tables(one_db):
    assert db.init_db()  # creates runs/incidents/report_submissions, must leave `reports` alone
    with one_db.connect() as c:
        assert c.execute(text("SELECT COUNT(*) FROM reports")).scalar() == 1
        tables = {r[0] for r in c.execute(text("SELECT name FROM sqlite_master WHERE type='table'"))}
    assert {"reports", "runs", "incidents", "report_submissions"} <= tables
    assert citizen_db.configured() and citizen_db.ping() is True
    assert [r["id"] for r in citizen_db.fetch_recent()] == ["cr-11111111-aaaa"]


async def test_get_reports_and_live_event_come_from_the_database(one_db, monkeypatch):
    import asyncio

    import routers.reports as reports_router
    from services import citizen_sync

    monkeypatch.setattr(reports_router, "report_store", ReportStore())  # not the global one other tests filled
    db.init_db()
    out = await reports_router.list_reports()
    assert [r["token"] for r in out] == ["cr-11111111-aaaa"]
    sub = out[0]["submission"]
    assert out[0]["urgency"] == "critical" and out[0]["run_id"] is None
    assert (sub["vulnerable"], sub["people_count"], sub["is_own_location"], sub["contact"]) == (["disabled"], 3, True, None)
    assert "555" not in json.dumps(out)

    published = []
    monkeypatch.setattr(citizen_sync.bus, "publish", lambda ev, data: published.append((ev, data)))
    citizen_sync.reset_sync()
    await citizen_sync.sync_once()
    assert published == []  # what is already in the table is not "new" at startup
    with one_db.begin() as c:
        c.execute(text("INSERT INTO reports VALUES ('22222222-bbbb','Need insulin','Kothrud',18.5,73.8,'medical','high','citizen',:t,'{}',NULL)"),
                  {"t": datetime.now(timezone.utc).replace(tzinfo=None)})
    await citizen_sync.sync_once()  # no active run: still announced to the dashboard
    assert [(e, d["token"]) for e, d in published] == [("report.new", "cr-22222222-bbbb")]
    await citizen_sync.sync_once()
    assert len(published) == 1  # not announced twice
