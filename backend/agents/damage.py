"""Damage Assessment Agent — cluster incidents into zones with severity."""

from __future__ import annotations

import math
from collections import defaultdict
from typing import Dict, List, Set

from config import get_settings
from services.llm import llm_service
from state import DamageAssessmentOutput, Incident, RunState, Severity, Zone, VerificationResult

CLUSTER_KM = 2.5


def _haversine_km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def _credibility_map(verifications: List[VerificationResult]) -> Dict[str, float]:
    return {v.incident_id: v.credibility for v in verifications}


def _cluster_incidents(
    incidents: List[Incident], cred: Dict[str, float]
) -> List[List[Incident]]:
    """Simple greedy clustering by proximity."""
    eligible = [i for i in incidents if cred.get(i.id, 0.5) >= 0.35]
    clusters: List[List[Incident]] = []
    assigned: Set[str] = set()

    for inc in sorted(eligible, key=lambda x: -cred.get(x.id, 0)):
        if inc.id in assigned:
            continue
        cluster = [inc]
        assigned.add(inc.id)
        for other in eligible:
            if other.id in assigned:
                continue
            if _haversine_km(inc.lat, inc.lng, other.lat, other.lng) <= CLUSTER_KM:
                cluster.append(other)
                assigned.add(other.id)
        clusters.append(cluster)
    return clusters


def _severity_for_cluster(cluster: List[Incident], cred: Dict[str, float]) -> Severity:
    count = len(cluster)
    critical = sum(1 for i in cluster if i.urgency.value == "critical")
    high_need = sum(1 for i in cluster if i.need_type.value in ("rescue", "medical", "evacuation"))
    avg_cred = sum(cred.get(i.id, 0.5) for i in cluster) / max(count, 1)

    score = count * 0.5 + critical * 2 + high_need * 1.5 + avg_cred
    if score >= 12:
        return Severity.CRITICAL
    if score >= 8:
        return Severity.HIGH
    if score >= 4:
        return Severity.MEDIUM
    return Severity.LOW


async def _summarize_zone(name: str, severity: Severity, count: int) -> str:
    if get_settings().effective_mock_mode:
        return (
            f"Zone {name}: {count} incidents assessed at {severity.value} severity "
            "based on incident density, urgency, and need types."
        )
    system = "Write one sentence summarizing a disaster damage zone."
    user = f"Zone {name}, severity {severity.value}, {count} incidents."
    try:
        return await llm_service.complete(system, user, max_tokens=80)
    except Exception:
        return f"Zone {name}: {severity.value} severity, {count} incidents."


async def run_damage_assessment(state: RunState) -> DamageAssessmentOutput:
    """Cluster incidents and compute zone severity in code; LLM summarizes."""
    cred = _credibility_map(state.verifications)
    clusters = _cluster_incidents(state.incidents, cred)
    zones: List[Zone] = []

    for idx, cluster in enumerate(clusters, start=1):
        center_lat = sum(i.lat for i in cluster) / len(cluster)
        center_lng = sum(i.lng for i in cluster) / len(cluster)
        severity = _severity_for_cluster(cluster, cred)
        name = f"Zone-{idx} ({cluster[0].location[:30]})"
        summary = await _summarize_zone(name, severity, len(cluster))
        zones.append(
            Zone(
                id=f"zone-{idx:03d}",
                name=name,
                center_lat=center_lat,
                center_lng=center_lng,
                severity=severity,
                incident_ids=[i.id for i in cluster],
                summary=summary,
            )
        )
    zones.sort(key=lambda z: list(Severity).index(z.severity), reverse=True)
    return DamageAssessmentOutput(zones=zones)


async def update_zones(state: RunState, new_ids: Set[str]) -> DamageAssessmentOutput:
    """Fold new incidents into the existing zones without renumbering them.

    An incident within CLUSTER_KM of a zone's centre joins that zone (centre and severity are
    recomputed); otherwise it starts a new zone with the next free id. Existing zone ids never
    change, so supply allocations and plan items that refer to them stay valid.
    """
    cred = _credibility_map(state.verifications)
    by_id = {i.id: i for i in state.incidents}
    zones = [z.model_copy(deep=True) for z in state.zones]
    next_n = max((int(z.id.rsplit("-", 1)[-1]) for z in zones if z.id.rsplit("-", 1)[-1].isdigit()), default=0) + 1

    for nid in sorted(new_ids):
        inc = by_id.get(nid)
        if inc is None or cred.get(nid, 0.5) < 0.35 or any(nid in z.incident_ids for z in zones):
            continue
        near = [z for z in zones if _haversine_km(inc.lat, inc.lng, z.center_lat, z.center_lng) <= CLUSTER_KM]
        if near:
            zone = min(near, key=lambda z: _haversine_km(inc.lat, inc.lng, z.center_lat, z.center_lng))
            zone.incident_ids.append(nid)
            members = [by_id[i] for i in zone.incident_ids if i in by_id]
            zone.center_lat = sum(m.lat for m in members) / len(members)
            zone.center_lng = sum(m.lng for m in members) / len(members)
            zone.severity = _severity_for_cluster(members, cred)
            zone.summary = await _summarize_zone(zone.name, zone.severity, len(members))
        else:
            name = f"Zone-{next_n} ({inc.location[:30]})"
            severity = _severity_for_cluster([inc], cred)
            zones.append(
                Zone(
                    id=f"zone-{next_n:03d}",
                    name=name,
                    center_lat=inc.lat,
                    center_lng=inc.lng,
                    severity=severity,
                    incident_ids=[nid],
                    summary=await _summarize_zone(name, severity, 1),
                )
            )
            next_n += 1
    zones.sort(key=lambda z: list(Severity).index(z.severity), reverse=True)
    return DamageAssessmentOutput(zones=zones)
