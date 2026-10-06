"""Reset Simulation: remove what the Simulation app created, and nothing else.

Simulation-created state is identified by tags the simulator's own actions put on it, never by guessing:

  * runs      RunState.simulated (set when the Simulation app starts a run via POST /scenario/start)
  * signals   metadata.simulated (the evidence controls, see services/sim_evidence.py)
  * incidents Incident.source == SIM (POST /incidents, the simulator's Add Incident form); these are also taken out
              of any real run they were added to, together with the plan items made for them
  * census    the hospital occupancy the simulator publishes (it only exists while a simulator is publishing)

Real runs, real incidents, real signals, real plans and citizen reports are not deleted or modified. If a real
incident's credibility had been moved by simulated evidence, it is scored again without that evidence.

Afterwards the newest real run (if any) becomes the active run again and every client is told to follow it.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Set

from services.bus import bus
from services.city import city_context, effective_city_slug
from services.events import event_bus
from services.hospital_state import hospital_store
from services.plan_payload import broadcast_plan
from services.resync import resync_from_db
from services.signals.store import signal_store
from services.store import run_store
from state import IncidentSource, RunState

log = logging.getLogger("sim_reset")


def _strip_incidents(run: RunState, ids: Set[str]) -> None:
    """Take these incidents, and everything the agents derived only for them, out of the run and its plan."""
    run.incidents = [i for i in run.incidents if i.id not in ids]
    run.verifications = [v for v in run.verifications if v.incident_id not in ids]
    run.rescue_queue = [r for r in run.rescue_queue if r.incident_id not in ids]
    run.hospital_assignments = [h for h in run.hospital_assignments if h.incident_id not in ids]

    gone_zones: Set[str] = set()
    kept = []
    for z in run.zones:
        left = [i for i in z.incident_ids if i not in ids]
        if len(left) != len(z.incident_ids) and not left:
            gone_zones.add(z.id)  # a zone made only of removed incidents goes with them
            continue
        kept.append(z.model_copy(update={"incident_ids": left}) if len(left) != len(z.incident_ids) else z)
    run.zones = kept
    run.supply_allocations = [s for s in run.supply_allocations if s.zone_id not in gone_zones]

    def route_gone(r: Any) -> bool:
        return r.assignment_id in ids or (r.assignment_type == "logistics" and r.assignment_id in gone_zones)

    run.routes = [r for r in run.routes if not route_gone(r)]

    dead_items = (
        {f"rescue-{i}" for i in ids} | {f"medical-{i}" for i in ids}
        | {f"route-{i}-rescue" for i in ids} | {f"route-{i}-medical" for i in ids}
        | {f"logistics-{z}" for z in gone_zones} | {f"route-{z}-logistics" for z in gone_zones}
    )
    if run.plan is not None:
        plan = run.plan
        plan.items = [i for i in plan.items if i.id not in dead_items]
        plan.rescue_queue = run.rescue_queue
        plan.hospital_assignments = run.hospital_assignments
        plan.supply_allocations = run.supply_allocations
        plan.routes = run.routes


async def _purge_sim_incidents(run: RunState) -> int:
    ids = {i.id for i in run.incidents if i.source == IncidentSource.SIM}
    if not ids:
        return 0
    from agents.communication import run_communication  # late import: keeps module import light

    async with run_store.run_lock(run.id):
        _strip_incidents(run, ids)
        try:  # alert drafts were written from the old plan: write them again from what is left
            with city_context(run.city):
                comm = await run_communication(run)
            run.alerts = comm.alerts
        except Exception:
            log.warning("could not rewrite alert drafts for run %s after reset", run.id, exc_info=True)
        run_store.update_run(run)
        broadcast_plan(run)
    run_store.delete_incidents(ids)
    return len(ids)


async def reset_simulation() -> Dict[str, Any]:
    from supervisor import rescore_incidents  # late import: supervisor imports a lot

    # 0. memory first agrees with the database again: whatever was deleted from it by hand must not survive here
    synced = resync_from_db()

    # 1. simulation runs go (memory and database). A pipeline still working on one can no longer write it back.
    sim_runs = [r for r in run_store.all_runs() if r.simulated]
    sim_incident_ids = {i.id for r in sim_runs for i in r.incidents}
    for run in sim_runs:
        run_store.remove_run(run.id)
        event_bus.forget(run.id)
    # Incidents that were only in a removed run. Citizen-database reports ("cr-...") stay: they belong to the citizens' table.
    still_used = {i.id for r in run_store.all_runs() for i in r.incidents}
    run_store.delete_incidents(i for i in sim_incident_ids - still_used if not i.startswith("cr-"))

    # 2. simulated evidence goes
    removed_signals = signal_store.remove_simulated()

    # 3. the simulator's hospital census and last world snapshot go
    hospital_store.reset()
    bus.latest_world = None

    # 4. simulation incidents added to a real run go from it
    purged = 0
    for run in run_store.all_runs():
        purged += await _purge_sim_incidents(run)

    # 5. real incidents whose credibility had been moved by simulated evidence are scored again without it
    rescored = 0
    gone = set(removed_signals)
    for run in run_store.all_runs():
        stale = {
            v.incident_id for v in run.verifications
            if gone.intersection(v.supported_by) or gone.intersection(v.contradicted_by)
        }
        if stale:
            rescored += len(await rescore_incidents(run.id, stale))

    # 6. every client follows the newest real run again (none: they follow nothing)
    active = run_store.get_active_run()
    if active is None:
        real = sorted(run_store.all_runs(), key=lambda r: r.created_at)
        active = real[-1] if real else None
        if active is not None:
            run_store.set_active(active.id)
        else:
            run_store.clear_active()
    city = effective_city_slug(active.city) if active else None
    removed_ids = [r.id for r in sim_runs]
    bus.publish("sim.reset", {"removed_run_ids": removed_ids, "run_id": active.id if active else None, "city": city})
    if active is not None:
        bus.publish("run.active", {"run_id": active.id, "city": city})
        broadcast_plan(active)

    return {
        "ok": True,
        "removed_runs": len(sim_runs),
        "removed_signals": len(removed_signals),
        "removed_incidents": purged,
        "rescored_incidents": rescored,
        "active_run_id": active.id if active else None,
        "db_sync": synced,
    }
