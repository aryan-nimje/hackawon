"""Supervisor Agent — orchestrates specialist agents with failure handling."""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from datetime import datetime
from typing import Any, Awaitable, Callable, List, Optional, Set

from agents.communication import run_communication
from agents.damage import run_damage_assessment, update_zones
from agents.detection import run_detection
from agents.logistics import run_logistics
from agents.medical import run_medical
from agents.rescue import run_rescue
from agents.route import run_route
from agents.verification import run_verification
from services.bus import bus
from services.city import city_context, effective_city_slug
from services.data_loader import is_scenario_city, load_hospitals
from services.events import event_bus
from services.hospital_state import hospital_store
from services.plan_payload import broadcast_plan
from services.store import run_store
from state import (
    ActivityEvent,
    AgentStatus,
    HospitalAssignment,
    Incident,
    IncidentSource,
    NeedType,
    PlanItem,
    PlanItemStatus,
    RescueAssignment,
    ResponsePlan,
    RouteInfo,
    RunState,
    RunStatus,
    SupplyAllocation,
    VerificationResult,
)

logger = logging.getLogger(__name__)

AGENT_TIMEOUT = 30.0


async def _emit(
    run_id: str, agent: str, status: AgentStatus, summary: str, duration_ms: int | None = None
) -> None:
    event = ActivityEvent(
        run_id=run_id,
        agent=agent,
        status=status,
        summary=summary,
        duration_ms=duration_ms,
        timestamp=datetime.utcnow(),
    )
    run = run_store.get_run(run_id)
    if run:
        run.activity_log.append(event)
        run_store.update_run(run)
    event_bus.publish(event)


def _announce_capacity(run_id: str, assignments: List[Any], prior: List[Any] | None = None) -> None:
    """Tell Authority (via sim_event on the bus) when a hospital fills up or a patient is diverted.

    `prior`: assignments made earlier in the same run; they count towards a hospital's load but are not
    announced again (used when one incident is added to a live plan).
    """
    # Live free beds when the simulator is running this run; `prior` then only counts patients not yet in a bed.
    beds = {h["id"]: (h["name"], h.get("beds_available", 0)) for h in hospital_store.overlay(load_hospitals(), run_id)}
    now = datetime.utcnow().isoformat() + "Z"
    counts: dict[str, int] = {}
    for a in [*hospital_store.unadmitted(run_id, prior or []), *assignments]:
        counts[a.hospital_id] = counts.get(a.hospital_id, 0) + 1
    touched = {a.hospital_id for a in assignments}
    counts = {k: v for k, v in counts.items() if k in touched}
    for hid, n in counts.items():
        name, free = beds.get(hid, (hid, 0))
        if n >= free:
            bus.publish("sim_event", {"kind": "hospital_full", "text": f"{name} is full ({n} patients assigned, {free} beds).", "at": now, "run_id": run_id})
    for a in assignments:
        if a.diverted_from:
            bus.publish("sim_event", {"kind": "patient_diverted", "text": f"Patient {a.incident_id} diverted from {a.diverted_from} to {a.hospital_name}.", "at": now, "incident_id": a.incident_id, "run_id": run_id})
        elif a.overflow:
            bus.publish("sim_event", {"kind": "patient_diverted", "text": f"Patient {a.incident_id}: all hospitals full, overflow at {a.hospital_name}. Escalate.", "at": now, "incident_id": a.incident_id, "run_id": run_id})


def _broadcast(state: RunState) -> None:
    """Push the current run snapshot to /bus/stream as plan.updated (plan is null until the end)."""
    run_store.update_run(state)
    broadcast_plan(state)


async def _run_agent(
    run_id: str,
    name: str,
    fn: Callable[[RunState], Awaitable[Any]],
    state: RunState,
    simulate_fail: bool = False,
) -> tuple[Any | None, bool]:
    """Run one agent with timeout and failure handling. Returns (result, failed)."""
    if simulate_fail:
        await _emit(run_id, name, AgentStatus.RUNNING, f"{name} starting (simulated failure)...")
        await asyncio.sleep(0.3)
        await _emit(run_id, name, AgentStatus.FAILED, f"{name} failed (simulated)", duration_ms=300)
        return None, True

    await _emit(run_id, name, AgentStatus.RUNNING, f"{name} started")
    start = time.monotonic()
    try:
        result = await asyncio.wait_for(fn(state), timeout=AGENT_TIMEOUT)
        ms = int((time.monotonic() - start) * 1000)
        await _emit(run_id, name, AgentStatus.COMPLETED, f"{name} completed", duration_ms=ms)
        return result, False
    except asyncio.TimeoutError:
        ms = int((time.monotonic() - start) * 1000)
        await _emit(run_id, name, AgentStatus.FAILED, f"{name} timed out", duration_ms=ms)
        logger.exception("Agent %s timed out", name)
        return None, True
    except Exception as exc:
        ms = int((time.monotonic() - start) * 1000)
        await _emit(run_id, name, AgentStatus.FAILED, f"{name} failed: {type(exc).__name__}", duration_ms=ms)
        logger.exception("Agent %s failed", name)
        return None, True


def _incomplete_placeholder(state: RunState, category: str, title: str) -> PlanItem:
    return PlanItem(
        id=f"{category}-incomplete",
        category=category,  # type: ignore[arg-type]
        title=title,
        description="Agent failed or timed out — section incomplete.",
        reasoning="Supervisor continued with partial results. Human review required.",
        incomplete=True,
    )


def _rescue_item(r: RescueAssignment, incomplete: bool) -> PlanItem:
    return PlanItem(
        id=f"rescue-{r.incident_id}",
        category="rescue",
        title=f"Rescue #{r.rank}: {r.incident_id}",
        description=f"Priority score {r.priority_score}",
        reasoning=r.explanation,
        incomplete=incomplete,
    )


def _medical_item(m: HospitalAssignment, incomplete: bool) -> PlanItem:
    return PlanItem(
        id=f"medical-{m.incident_id}",
        category="medical",
        title=f"Hospital: {m.hospital_name}",
        description=f"Distance {m.distance_km} km",
        reasoning=m.explanation,
        incomplete=incomplete,
    )


def _logistics_item(s: SupplyAllocation, incomplete: bool) -> PlanItem:
    return PlanItem(
        id=f"logistics-{s.zone_id}",
        category="logistics",
        title=f"Supplies to {s.zone_id}",
        description=str(s.items),
        reasoning=s.explanation + (" Shortages: " + ", ".join(s.shortage_flags) if s.shortage_flags else ""),
        incomplete=incomplete,
    )


def _route_item(rt: RouteInfo, incomplete: bool) -> PlanItem:
    return PlanItem(
        id=f"route-{rt.assignment_id}-{rt.assignment_type}",
        category="route",
        title=f"Route ({rt.assignment_type}): {rt.assignment_id}",
        description=f"{rt.distance_km} km, ~{rt.duration_min} min",
        reasoning=rt.explanation,
        incomplete=incomplete,
    )


def _build_plan(state: RunState) -> ResponsePlan:
    items: List[PlanItem] = []

    for r in state.rescue_queue:
        items.append(_rescue_item(r, "rescue" in state.incomplete_sections))

    for m in state.hospital_assignments:
        items.append(_medical_item(m, "medical" in state.incomplete_sections))

    for s in state.supply_allocations:
        items.append(_logistics_item(s, "logistics" in state.incomplete_sections))

    for rt in state.routes:
        items.append(_route_item(rt, "route" in state.incomplete_sections))

    section_defaults = {
        "rescue": ("rescue", "Rescue queue unavailable"),
        "medical": ("medical", "Hospital assignments unavailable"),
        "logistics": ("logistics", "Supply allocations unavailable"),
        "route": ("route", "Route planning unavailable"),
        "communication": ("communication", "Alert drafts unavailable"),
    }
    existing_categories = {i.category for i in items}
    for section in state.incomplete_sections:
        cat, title = section_defaults.get(section, (section, f"{section} unavailable"))
        if cat not in existing_categories:
            items.append(_incomplete_placeholder(state, cat, title))

    return ResponsePlan(
        run_id=state.id,
        rescue_queue=state.rescue_queue,
        hospital_assignments=state.hospital_assignments,
        supply_allocations=state.supply_allocations,
        routes=state.routes,
        items=items,
        is_final=False,
    )


async def run_pipeline(state: RunState, report_ids: List[str] | None = None) -> RunState:
    """Execute the full agent pipeline for a run (holds the run lock, so incident updates wait for it)."""
    async with run_store.run_lock(state.id):
        with city_context(state.city):  # every agent in this run sees this run's city
            return await _run_pipeline(state, report_ids)


async def _run_pipeline(state: RunState, report_ids: List[str] | None = None) -> RunState:
    run_id = state.id
    state.status = RunStatus.RUNNING
    run_store.update_run(state)
    failures = set(state.simulate_failures)

    # Detection (skip if incidents already loaded via scenario replay)
    if state.incidents:
        await _emit(
            run_id,
            "detection",
            AgentStatus.COMPLETED,
            f"Using {len(state.incidents)} incidents from scenario replay",
            duration_ms=0,
        )
        _broadcast(state)
    else:
        det, failed = await _run_agent(run_id, "detection", run_detection, state)
        if not failed and det:
            state.incidents.extend(det.incidents)
            for inc in det.incidents:
                run_store.upsert_incident(inc)
            state.agent_outputs["detection"] = det.model_dump()
        elif failed:
            state.incomplete_sections.append("detection")
        _broadcast(state)

    # Verification
    ver, failed = await _run_agent(run_id, "verification", run_verification, state)
    if not failed and ver:
        state.verifications = ver.results
        state.agent_outputs["verification"] = ver.model_dump()
    elif failed:
        state.incomplete_sections.append("verification")
    _broadcast(state)

    # Damage Assessment
    dmg, failed = await _run_agent(run_id, "damage_assessment", run_damage_assessment, state)
    if not failed and dmg:
        state.zones = dmg.zones
        state.agent_outputs["damage_assessment"] = dmg.model_dump()
    elif failed:
        state.incomplete_sections.append("damage_assessment")
    _broadcast(state)

    # Parallel: Rescue, Medical, Logistics
    await _emit(run_id, "parallel_ops", AgentStatus.RUNNING, "Rescue, Medical, Logistics running in parallel")

    async def _parallel_rescue() -> None:
        r, f = await _run_agent(
            run_id, "rescue", run_rescue, state, simulate_fail="rescue" in failures
        )
        if not f and r:
            state.rescue_queue = r.queue
            state.agent_outputs["rescue"] = r.model_dump()
        elif f:
            state.incomplete_sections.append("rescue")
        _broadcast(state)

    async def _parallel_medical() -> None:
        m, f = await _run_agent(
            run_id, "medical", run_medical, state, simulate_fail="medical" in failures
        )
        if not f and m:
            state.hospital_assignments = m.assignments
            state.agent_outputs["medical"] = m.model_dump()
            _announce_capacity(run_id, m.assignments)
        elif f:
            state.incomplete_sections.append("medical")
        _broadcast(state)

    async def _parallel_logistics() -> None:
        lg, f = await _run_agent(
            run_id, "logistics", run_logistics, state, simulate_fail="logistics" in failures
        )
        if not f and lg:
            state.supply_allocations = lg.allocations
            state.agent_outputs["logistics"] = lg.model_dump()
        elif f:
            state.incomplete_sections.append("logistics")
        _broadcast(state)

    await asyncio.gather(_parallel_rescue(), _parallel_medical(), _parallel_logistics())
    await _emit(run_id, "parallel_ops", AgentStatus.COMPLETED, "Parallel agents finished")

    # Route (with optional one revision loop)
    route_result, route_failed = await _run_agent(
        run_id, "route", run_route, state, simulate_fail="route" in failures
    )
    if not route_failed and route_result:
        state.routes = route_result.routes
        state.agent_outputs["route"] = route_result.model_dump()

        if route_result.needs_revision and state.revision_count == 0:
            state.revision_count += 1
            await _emit(
                run_id,
                "supervisor",
                AgentStatus.RUNNING,
                f"Route revision triggered: {route_result.revision_reason}",
            )
            # Re-run logistics with adjusted priority (one revision pass)
            lg, f = await _run_agent(run_id, "logistics", run_logistics, state)
            if not f and lg:
                state.supply_allocations = lg.allocations
            _broadcast(state)
            rt2, _ = await _run_agent(run_id, "route", run_route, state)
            if rt2:
                state.routes = rt2.routes
            _broadcast(state)
            await _emit(run_id, "supervisor", AgentStatus.COMPLETED, "Route revision complete")
    elif route_failed:
        state.incomplete_sections.append("route")
    _broadcast(state)

    # Communication
    comm, comm_failed = await _run_agent(run_id, "communication", run_communication, state)
    if not comm_failed and comm:
        state.alerts = comm.alerts
        state.agent_outputs["communication"] = comm.model_dump()
    elif comm_failed:
        state.incomplete_sections.append("communication")
    _broadcast(state)

    state.plan = _build_plan(state)
    state.status = RunStatus.COMPLETED
    state.completed_at = datetime.utcnow()
    run_store.update_run(state)
    await _emit(run_id, "supervisor", AgentStatus.COMPLETED, "Response plan draft ready for human review")
    _broadcast(state)  # first payload with a non-null plan (and the final activity_log entry)
    return state


def create_run(simulate_failures: List[str] | None = None, city: str | None = None, simulated: bool = False) -> RunState:
    run = RunState(
        id=str(uuid.uuid4()),
        simulate_failures=simulate_failures or [],
        city=city,
        simulated=simulated,
    )
    return run_store.create_run(run)


async def start_scenario_replay(
    replay_speed: float = 1.0,
    simulate_failures: List[str] | None = None,
    city: str | None = None,
    simulated: bool = False,
) -> RunState:
    """Start scenario: replay reports over time then run pipeline.

    `city` (slug): the city this run plans for. The shipped scenario is Pune's, so for any other city nothing is
    replayed: the run starts empty (weather alert only) and incidents come from the simulator via POST /incidents.
    """
    from services.data_loader import load_scenario

    scenario = load_scenario()
    run = create_run(simulate_failures, city, simulated)
    run_id = run.id
    run.status = RunStatus.RUNNING
    run_store.update_run(run)
    # This run is now the one every client follows; older runs are hidden, not deleted.
    run_store.set_active(run_id)
    bus.publish("run.active", {"run_id": run_id, "city": effective_city_slug(run.city)})

    seen_ids: set[str] = set()
    for event in (scenario.get("replay_events", []) if is_scenario_city(run.city) else []):
        delay = event.get("offset_seconds", 0) / replay_speed
        if delay > 0:
            await asyncio.sleep(min(delay, 2.0))  # cap wait for demo
        ids = event.get("report_ids", [])
        new_ids = [i for i in ids if i not in seen_ids]
        if not new_ids:
            continue
        seen_ids.update(new_ids)

        async def _detect_batch(s: RunState, batch: List[str] = new_ids) -> Any:
            return await run_detection(s, report_ids=batch)

        with city_context(run.city):
            det, failed = await _run_agent(run_id, "detection", _detect_batch, run)
        if not failed and det:
            existing = {i.id for i in run.incidents}
            for inc in det.incidents:
                if inc.id not in existing:
                    run.incidents.append(inc)
                    run_store.upsert_incident(inc)
            run_store.update_run(run)
        _broadcast(run)

    return await run_pipeline(run)


# ───────────────────────── manual incidents ─────────────────────────
# A manual incident goes through the same agents, with the same timeout / failure handling, as the
# scenario's incidents. It is folded into the run's existing plan: nothing already in the plan (items,
# approvals, routes, allocations) is reset.

_incident_tasks: set = set()


async def register_incident(run: RunState, incident: Incident) -> None:
    """Make the incident part of the run right away (visible to every client); planning follows."""
    if any(i.id == incident.id for i in run.incidents):
        return
    run.incidents.append(incident)
    run_store.upsert_incident(incident)
    run_store.update_run(run)
    await _emit(
        run.id,
        "detection",
        AgentStatus.COMPLETED,
        f"{'Citizen report' if incident.source == IncidentSource.CITIZEN else 'Manual incident'} {incident.id} received "
        f"({incident.urgency.value} {incident.need_type.value})",
        duration_ms=0,
    )
    _broadcast(run)


def schedule_incident_planning(run_id: str, incident_id: str) -> None:
    task = asyncio.create_task(plan_new_incident(run_id, incident_id))
    _incident_tasks.add(task)  # keep a reference so it is not garbage-collected mid-run
    task.add_done_callback(_incident_tasks.discard)


async def start_run_for_incident(incident: Incident, city: str | None = None) -> RunState:
    """No run exists yet: start one around this incident and run the normal pipeline on it."""
    run = create_run(city=city)
    run.status = RunStatus.RUNNING
    run.incidents.append(incident)
    run_store.upsert_incident(incident)
    run_store.update_run(run)
    run_store.set_active(run.id)
    bus.publish("run.active", {"run_id": run.id, "city": effective_city_slug(run.city)})
    task = asyncio.create_task(run_pipeline(run))
    _incident_tasks.add(task)
    task.add_done_callback(_incident_tasks.discard)
    return run


async def plan_new_incident(run_id: str, incident_id: str) -> RunState | None:
    """Plan one incident into the run's existing plan. Waits for any pipeline / earlier update on the run."""
    async with run_store.run_lock(run_id):
        run = run_store.get_run(run_id)
        inc = next((i for i in run.incidents if i.id == incident_id), None) if run else None
        if run is None or inc is None:
            return None
        if run.plan is None:
            # The pipeline has not built a plan yet; it reads run.incidents itself and will include this one.
            return run
        try:
            with city_context(run.city):
                await _plan_incident(run, inc)
        except Exception as exc:
            logger.exception("Planning incident %s failed", incident_id)
            await _emit(run_id, "supervisor", AgentStatus.FAILED, f"Could not plan {incident_id}: {type(exc).__name__}")
        return run


async def rescore_incidents(run_id: str, incident_ids: Set[str]) -> List[VerificationResult]:
    """Score these incidents again with the normal Verification Agent (the current signal store decides the result),
    replace their entries in the run and broadcast. Used when evidence is added or removed after the pipeline ran.
    Waits for the run lock, so it never interleaves with the pipeline or an incident update."""
    async with run_store.run_lock(run_id):
        run = run_store.get_run(run_id)
        if run is None:
            return []
        with city_context(run.city):
            out = await run_verification(run, set(incident_ids))
        fresh = {v.incident_id: v for v in out.results}
        kept = [fresh.pop(v.incident_id, v) for v in run.verifications]
        run.verifications = kept + list(fresh.values())
        run_store.update_run(run)
        _broadcast(run)
        return list(out.results)


def _mark_incomplete(state: RunState, section: str) -> None:
    if section not in state.incomplete_sections:
        state.incomplete_sections.append(section)


async def _plan_incident(run: RunState, inc: Incident) -> None:
    run_id = run.id
    ids = {inc.id}
    failures = set(run.simulate_failures)
    failed_now: List[str] = []
    await _emit(run_id, "supervisor", AgentStatus.RUNNING, f"Planning {inc.id} into the existing plan")

    # Verification
    if not any(v.incident_id == inc.id for v in run.verifications):
        ver, failed = await _run_agent(run_id, "verification", lambda s: run_verification(s, ids), run)
        if not failed and ver:
            run.verifications.extend(ver.results)
        elif failed:
            _mark_incomplete(run, "verification")
        _broadcast(run)

    # Damage assessment: join or start a zone, existing zone ids stay
    known_zones = {z.id for z in run.zones}
    dmg, failed = await _run_agent(run_id, "damage_assessment", lambda s: update_zones(s, ids), run)
    if not failed and dmg:
        run.zones = dmg.zones
    elif failed:
        _mark_incomplete(run, "damage_assessment")
    new_zone_ids = {z.id for z in run.zones} - known_zones
    _broadcast(run)

    # Rescue, Medical, Logistics (only the ones this incident needs), in parallel
    new_rescue: List[RescueAssignment] = []
    new_medical: List[HospitalAssignment] = []
    new_supply: List[SupplyAllocation] = []

    async def _rescue() -> None:
        r, f = await _run_agent(run_id, "rescue", lambda s: run_rescue(s, ids), run, simulate_fail="rescue" in failures)
        if not f and r:
            run.rescue_queue = r.queue
            new_rescue.extend(a for a in r.queue if a.incident_id == inc.id)
        elif f:
            _mark_incomplete(run, "rescue")
            failed_now.append("rescue")
        _broadcast(run)

    async def _medical() -> None:
        prior = list(run.hospital_assignments)
        m, f = await _run_agent(run_id, "medical", lambda s: run_medical(s, ids), run, simulate_fail="medical" in failures)
        if not f and m:
            run.hospital_assignments = [*prior, *m.assignments]
            new_medical.extend(m.assignments)
            _announce_capacity(run_id, m.assignments, prior=prior)
        elif f:
            _mark_incomplete(run, "medical")
            failed_now.append("medical")
        _broadcast(run)

    async def _logistics() -> None:
        lg, f = await _run_agent(run_id, "logistics", lambda s: run_logistics(s, new_zone_ids), run, simulate_fail="logistics" in failures)
        if not f and lg:
            run.supply_allocations = [*run.supply_allocations, *lg.allocations]
            new_supply.extend(lg.allocations)
        elif f:
            _mark_incomplete(run, "logistics")
            failed_now.append("logistics")
        _broadcast(run)

    jobs = []
    if inc.need_type in (NeedType.RESCUE, NeedType.EVACUATION) and not any(r.incident_id == inc.id for r in run.rescue_queue):
        jobs.append(_rescue())
    if inc.need_type == NeedType.MEDICAL and not any(h.incident_id == inc.id for h in run.hospital_assignments):
        jobs.append(_medical())
    if new_zone_ids:
        jobs.append(_logistics())
    if jobs:
        await _emit(run_id, "parallel_ops", AgentStatus.RUNNING, "Rescue, Medical, Logistics running in parallel")
        await asyncio.gather(*jobs)
        await _emit(run_id, "parallel_ops", AgentStatus.COMPLETED, "Parallel agents finished")

    # Route: only for what is new (every new rescue site is routed, as in the full pipeline)
    new_rescue_routed = list(new_rescue)
    new_routes: List[RouteInfo] = []
    if new_rescue_routed or new_medical or new_supply:
        def _scoped(s: RunState) -> RunState:
            return s.model_copy(update={
                "rescue_queue": new_rescue_routed, "hospital_assignments": new_medical, "supply_allocations": new_supply,
            })

        route_result, route_failed = await _run_agent(
            run_id, "route", lambda s: run_route(_scoped(s)), run, simulate_fail="route" in failures
        )
        if not route_failed and route_result:
            new_routes = route_result.routes
            if route_result.needs_revision and run.revision_count == 0:
                run.revision_count += 1
                await _emit(run_id, "supervisor", AgentStatus.RUNNING, f"Route revision triggered: {route_result.revision_reason}")
                rt2, _ = await _run_agent(run_id, "route", lambda s: run_route(_scoped(s)), run)
                if rt2:
                    new_routes = rt2.routes
                await _emit(run_id, "supervisor", AgentStatus.COMPLETED, "Route revision complete")
            replaced = {(r.assignment_id, r.assignment_type) for r in new_routes}
            run.routes = [r for r in run.routes if (r.assignment_id, r.assignment_type) not in replaced] + new_routes
        elif route_failed:
            _mark_incomplete(run, "route")
            failed_now.append("route")
        _broadcast(run)

    # Communication: drafts are rewritten from the updated plan
    comm, comm_failed = await _run_agent(run_id, "communication", run_communication, run)
    if not comm_failed and comm:
        run.alerts = comm.alerts
    elif comm_failed:
        _mark_incomplete(run, "communication")
        failed_now.append("communication")

    # Fold into the plan: add new items, leave existing ones (and their approvals) alone
    plan = run.plan
    assert plan is not None
    existing = {i.id: i for i in plan.items}
    added: List[PlanItem] = []

    def _add(item: PlanItem) -> None:
        if item.id not in existing:
            plan.items.append(item)
            existing[item.id] = item
            added.append(item)

    for r in run.rescue_queue:
        item = _rescue_item(r, "rescue" in run.incomplete_sections)
        if r.incident_id == inc.id:
            _add(item)
        elif (cur := existing.get(item.id)) and cur.status == PlanItemStatus.PENDING:
            cur.title, cur.description, cur.reasoning = item.title, item.description, item.reasoning  # rank may have moved
    for m in new_medical:
        _add(_medical_item(m, "medical" in run.incomplete_sections))
    for sa in new_supply:
        _add(_logistics_item(sa, "logistics" in run.incomplete_sections))
    for rt in new_routes:
        _add(_route_item(rt, "route" in run.incomplete_sections))
    section_titles = {
        "rescue": "Rescue queue unavailable", "medical": "Hospital assignments unavailable",
        "logistics": "Supply allocations unavailable", "route": "Route planning unavailable",
        "communication": "Alert drafts unavailable",
    }
    for section in failed_now:
        if f"{section}-incomplete" not in existing:
            _add(_incomplete_placeholder(run, section, section_titles[section]))

    plan.rescue_queue = run.rescue_queue
    plan.hospital_assignments = run.hospital_assignments
    plan.supply_allocations = run.supply_allocations
    plan.routes = run.routes
    if any(i.status == PlanItemStatus.PENDING for i in added):
        # new work needs a human decision: the plan is no longer fully approved
        plan.is_final = False
        plan.approved_at = None

    run_store.update_run(run)
    await _emit(
        run_id, "supervisor", AgentStatus.COMPLETED,
        f"Plan updated for {inc.id}: {len(added)} new item(s) awaiting review",
    )
    _broadcast(run)
