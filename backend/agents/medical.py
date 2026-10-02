"""Medical Agent — match medical cases to nearest hospitals with capacity."""

from __future__ import annotations

import math
from typing import Dict, List

from config import get_settings
from services.data_loader import load_hospitals
from services.llm import llm_service
from state import HospitalAssignment, Incident, MedicalOutput, NeedType, RunState

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


def _best_hospital(incident: Incident, hospitals: List[Dict], assigned: Dict[str, int]) -> tuple[Dict, float, bool]:
    vulnerable = incident.raw_metadata.get("vulnerable", []) if incident.raw_metadata else []
    best = hospitals[0]
    best_dist = float("inf")
    best_match = False

    for h in hospitals:
        if h.get("status") == "closed":
            continue
        beds = h.get("beds_available", 0) - assigned.get(h["id"], 0)
        if beds <= 0:
            continue
        dist = _haversine_km(incident.lat, incident.lng, h["lat"], h["lng"])
        specialty_match = False
        for spec, tags in SPECIALTY_MAP.items():
            if spec in h.get("specialties", []) and any(t in vulnerable for t in tags):
                specialty_match = True
                dist *= 0.8
        if incident.need_type == NeedType.MEDICAL and "trauma" in h.get("specialties", []):
            dist *= 0.9
        if dist < best_dist:
            best_dist = dist
            best = h
            best_match = specialty_match
    return best, best_dist, best_match


async def _explain(incident: Incident, hospital: Dict, dist: float, match: bool) -> str:
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


async def run_medical(state: RunState) -> MedicalOutput:
    """Match medical incidents to hospitals based on proximity and capacity."""
    cred_map = {v.incident_id: v.credibility for v in state.verifications}
    hospitals = load_hospitals()
    assigned: Dict[str, int] = {}
    assignments: List[HospitalAssignment] = []

    medical_incidents = [
        i
        for i in state.incidents
        if i.need_type == NeedType.MEDICAL and cred_map.get(i.id, 0) >= 0.35
    ]

    for inc in medical_incidents:
        hospital, dist, match = _best_hospital(inc, hospitals, assigned)
        assigned[hospital["id"]] = assigned.get(hospital["id"], 0) + 1
        explanation = await _explain(inc, hospital, dist, match)
        assignments.append(
            HospitalAssignment(
                incident_id=inc.id,
                hospital_id=hospital["id"],
                hospital_name=hospital["name"],
                distance_km=round(dist, 2),
                explanation=explanation,
                specialty_match=match,
            )
        )
    return MedicalOutput(assignments=assignments)
