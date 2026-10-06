"""Medical Agent — match medical cases to nearest hospitals with capacity.

Capacity is live when the simulator is publishing a census (see services/hospital_state.py): a hospital
is full when every bed is occupied, and a bed comes back when its patient's stay ends.
"""

from __future__ import annotations

import math
import random
from typing import Dict, List, Optional

from config import get_settings
from services.data_loader import load_hospitals
from services.hospital_state import hospital_store
from services.llm import llm_service
from state import HospitalAssignment, Incident, MedicalOutput, NeedType, PlanItemStatus, RunState, Urgency

SPECIALTY_MAP = {
    "pediatrics": ["children", "infant"],
    "neonatal": ["infant"],
    "burn": ["burn"],
    "trauma": ["medical_needs"],
}


def _haversine_km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


UNAVAILABLE_STATUS = {"closed", "offline"}

# Expected length of stay in simulated minutes, by urgency (the simulator keeps the bed for this long).
STAY_MIN_RANGE = {
    Urgency.CRITICAL: (90.0, 150.0),
    Urgency.HIGH: (60.0, 110.0),
    Urgency.MEDIUM: (40.0, 70.0),
    Urgency.LOW: (20.0, 45.0),
}
# Vulnerable patients stay longer.
VULNERABLE_STAY_FACTOR = 1.2


def _expected_stay_min(incident: Incident) -> float:
    """How long this patient will occupy a bed. Deterministic per incident, so replans give the same answer."""
    lo, hi = STAY_MIN_RANGE.get(incident.urgency, STAY_MIN_RANGE[Urgency.MEDIUM])
    stay = random.Random(incident.id).uniform(lo, hi)
    if incident.raw_metadata and incident.raw_metadata.get("vulnerable"):
        stay *= VULNERABLE_STAY_FACTOR
    return round(stay, 1)


def _rejected_incident_ids(state: RunState) -> set[str]:
    """Incidents whose hospital item Authority rejected: they will not arrive, so they hold no bed."""
    if state.plan is None:
        return set()
    return {
        i.id[len("medical-"):]
        for i in state.plan.items
        if i.category == "medical" and i.id.startswith("medical-") and i.status == PlanItemStatus.REJECTED
    }


def _free_beds(h: Dict, assigned: Dict[str, int]) -> int:
    if h.get("status") == "full":
        return 0
    return h.get("beds_available", 0) - assigned.get(h["id"], 0)


def _rank_key(incident: Incident, h: Dict, vulnerable: List[str]) -> tuple[float, float, bool]:
    """(weighted score used for ranking, true distance in km, specialty match). Lower score is better."""
    dist = _haversine_km(incident.lat, incident.lng, h["lat"], h["lng"])
    score = dist
    match = False
    for spec, tags in SPECIALTY_MAP.items():
        if spec in h.get("specialties", []) and any(t in vulnerable for t in tags):
            match = True
            score *= 0.8
    if incident.need_type == NeedType.MEDICAL and "trauma" in h.get("specialties", []):
        score *= 0.9
    return score, dist, match


def _best_hospital(
    incident: Incident, hospitals: List[Dict], assigned: Dict[str, int]
) -> Optional[tuple[Dict, float, bool, Optional[Dict], bool]]:
    """Pick a hospital. Returns (hospital, distance_km, specialty_match, diverted_from, overflow) or None.

    The nominal choice is the best-ranked operational hospital ignoring capacity. If it has no free
    beds the patient is diverted to the next best one that does. If every operational hospital is full,
    the nominal one is used and flagged as overflow. Never falls back to an arbitrary hospital.
    """
    vulnerable = incident.raw_metadata.get("vulnerable", []) if incident.raw_metadata else []
    operational = [h for h in hospitals if h.get("status") not in UNAVAILABLE_STATUS]
    if not operational:
        return None
    ranked = sorted(((*_rank_key(incident, h, vulnerable), h) for h in operational), key=lambda t: t[0])
    nominal = ranked[0]
    chosen = next((t for t in ranked if _free_beds(t[3], assigned) > 0), None)
    if chosen is None:
        _, dist, match, hosp = nominal
        return hosp, dist, match, None, True
    _, dist, match, hosp = chosen
    diverted_from = nominal[3] if nominal[3]["id"] != hosp["id"] else None
    return hosp, dist, match, diverted_from, False


async def _explain(
    incident: Incident, hospital: Dict, dist: float, match: bool, diverted_from: Optional[Dict] = None, overflow: bool = False
) -> str:
    if overflow:
        return (
            f"All hospitals are at capacity. Assigned to nearest, {hospital['name']} ({dist:.1f} km), as overflow — "
            "escalate to Authority for surge beds or transfers."
        )
    if diverted_from:
        return (
            f"{diverted_from['name']} is full. Diverted to {hospital['name']} ({dist:.1f} km), "
            "the next nearest hospital with free beds."
        )
    if get_settings().effective_mock_mode:
        return (
            f"Assigned to {hospital['name']} ({dist:.1f} km) — "
            f"{'specialty match' if match else 'nearest available capacity'}."
        )
    system = "One sentence explaining hospital assignment."
    user = f"Case: {incident.text[:80]}. Hospital: {hospital['name']}, {dist:.1f}km."
    try:
        return await llm_service.complete(system, user, max_tokens=60)
    except Exception:
        return f"Assigned to {hospital['name']} ({dist:.1f} km)."


async def run_medical(state: RunState, only_ids: set[str] | None = None) -> MedicalOutput:
    """Match medical incidents to hospitals based on proximity and capacity.

    `only_ids`: when given (a new incident joins a live plan), only those incidents are assigned, and
    beds already taken by `state.hospital_assignments` count against capacity, so the same
    diversion / overflow rules apply. The output then holds just the new assignments.
    """
    cred_map = {v.incident_id: v.credibility for v in state.verifications}
    # Live occupancy from the simulator (free beds now, hospital status) when it is running this run;
    # otherwise the static layer data. Patients already in a bed are in the occupancy, so only patients
    # assigned but not admitted yet reserve a bed on top of it.
    hospitals = hospital_store.overlay(load_hospitals(), state.id)
    assigned: Dict[str, int] = {}
    if only_ids is not None:
        assigned = hospital_store.reserved(state.id, state.hospital_assignments, _rejected_incident_ids(state))
    assignments: List[HospitalAssignment] = []

    medical_incidents = [
        i
        for i in state.incidents
        if i.need_type == NeedType.MEDICAL
        and cred_map.get(i.id, 0) >= 0.35
        and (only_ids is None or i.id in only_ids)
    ]

    for inc in medical_incidents:
        pick = _best_hospital(inc, hospitals, assigned)
        if pick is None:  # every hospital is offline: nothing to assign, supervisor flags the gap
            continue
        hospital, dist, match, diverted_from, overflow = pick
        assigned[hospital["id"]] = assigned.get(hospital["id"], 0) + 1
        explanation = await _explain(inc, hospital, dist, match, diverted_from, overflow)
        assignments.append(
            HospitalAssignment(
                incident_id=inc.id,
                hospital_id=hospital["id"],
                hospital_name=hospital["name"],
                distance_km=round(dist, 2),
                explanation=explanation,
                specialty_match=match,
                diverted_from=diverted_from["name"] if diverted_from else None,
                overflow=overflow,
                expected_stay_min=_expected_stay_min(inc),
            )
        )
    return MedicalOutput(assignments=assignments)
