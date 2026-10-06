"""Follow the `reports` table the citizens write to.

Every tick, any report not seen before is announced to the dashboard as `report.new` (so it shows up live even
when no run is active). If a run is active the report is also planned into it: same path as a manual incident
(`register_incident` + `schedule_incident_planning`), so it appears at once and is planned into the existing
plan as pending items. Detection reads the same table when a run starts.
"""

from __future__ import annotations

import asyncio
import logging

from agents.detection import _report_to_incident
from config import get_settings
from services import citizen_db
from services.bus import bus
from services.store import run_store
from state import RunStatus

log = logging.getLogger("citizen_sync")
MAX_PER_TICK = 25  # bounds planning load if a burst arrives; the rest follow on the next tick
_announced: set | None = None  # ids already sent to the dashboard; None until the first tick has looked


def reset_sync() -> None:
    global _announced
    _announced = None


async def sync_once() -> int:
    """Ingest unseen citizen reports into the active run. Returns how many were added."""
    import supervisor  # late import: supervisor imports a lot, and this module is loaded by main

    global _announced
    reports = await asyncio.to_thread(citizen_db.fetch_recent)
    if _announced is None:
        # First look after startup: what is already in the table is not "new". GET /reports serves it.
        _announced = {r["id"] for r in reports}
    else:
        for raw in reversed(reports):
            if raw["id"] not in _announced:
                _announced.add(raw["id"])
                bus.publish("report.new", citizen_db.to_citizen_report(raw))
    run = run_store.get_active_run()
    if run is None or run.status == RunStatus.FAILED:
        return 0
    known = {i.id for i in run.incidents}
    added = 0
    for raw in reversed(reports):  # fetch_recent is newest-first; plan oldest-first
        if raw["id"] in known:
            continue
        if added >= MAX_PER_TICK:
            break
        incident = _report_to_incident(raw)
        await supervisor.register_incident(run, incident)
        supervisor.schedule_incident_planning(run.id, incident.id)
        added += 1
    if added:
        log.info("citizen sync: %d new report(s) added to run %s", added, run.id)
    return added


async def poll_loop() -> None:
    interval = get_settings().citizen_poll_seconds
    while True:
        try:
            await sync_once()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("citizen sync failed")
        await asyncio.sleep(interval)
