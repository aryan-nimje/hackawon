"""Supervisor Agent — orchestrates specialist agents with failure handling."""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from datetime import datetime
from typing import Any, Awaitable, Callable, List, Optional

from agents.communication import run_communication
from agents.damage import run_damage_assessment
from agents.detection import run_detection
from agents.logistics import run_logistics
from agents.medical import run_medical
from agents.rescue import run_rescue
from agents.route import run_route
from agents.verification import run_verification
from services.events import event_bus
from services.plan_payload import broadcast_plan
from services.store import run_store
from state import (
    ActivityEvent,
    AgentStatus,
    PlanItem,
    PlanItemStatus,
    ResponsePlan,
    RunState,
    RunStatus,
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


def _build_plan(state: RunState) -> ResponsePlan:
    items: List[PlanItem] = []

    for r in state.rescue_queue:
        incomplete = "rescue" in state.incomplete_sections
        items.append(
            PlanItem(
                id=f"rescue-{r.incident_id}",
                category="rescue",
                title=f"Rescue #{r.rank}: {r.incident_id}",
                description=f"Priority score {r.priority_score}",
                reasoning=r.explanation,
                incomplete=incomplete,
            )
        )

    for m in state.hospital_assignments:
        incomplete = "medical" in state.incomplete_sections
        items.append(
            PlanItem(
                id=f"medical-{m.incident_id}",
                category="medical",
                title=f"Hospital: {m.hospital_name}",
                description=f"Distance {m.distance_km} km",
                reasoning=m.explanation,
                incomplete=incomplete,
            )
        )

    for s in state.supply_allocations:
        incomplete = "logistics" in state.incomplete_sections
        items.append(
            PlanItem(
                id=f"logistics-{s.zone_id}",
                category="logistics",
                title=f"Supplies to {s.zone_id}",
                description=str(s.items),
                reasoning=s.explanation + (" Shortages: " + ", ".join(s.shortage_flags) if s.shortage_flags else ""),
                incomplete=incomplete,
            )
        )

    for rt in state.routes:
        items.append(
            PlanItem(
                id=f"route-{rt.assignment_id}-{rt.assignment_type}",
                category="route",
                title=f"Route ({rt.assignment_type}): {rt.assignment_id}",
                description=f"{rt.distance_km} km, ~{rt.duration_min} min",
                reasoning=rt.explanation,
                incomplete="route" in state.incomplete_sections,
            )
        )

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
    """Execute the full agent pipeline for a run."""
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


def create_run(simulate_failures: List[str] | None = None) -> RunState:
    run = RunState(
        id=str(uuid.uuid4()),
        simulate_failures=simulate_failures or [],
    )
    return run_store.create_run(run)


async def start_scenario_replay(
    replay_speed: float = 1.0,
    simulate_failures: List[str] | None = None,
) -> RunState:
    """Start scenario: replay reports over time then run pipeline."""
    from services.data_loader import load_scenario

    scenario = load_scenario()
    run = create_run(simulate_failures)
    run_id = run.id
    run.status = RunStatus.RUNNING
    run_store.update_run(run)

    seen_ids: set[str] = set()
    for event in scenario.get("replay_events", []):
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
