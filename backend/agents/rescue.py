"""Rescue Agent — ranked rescue queue with priority formula in code."""

from __future__ import annotations

from datetime import datetime
from typing import Dict, List

from config import get_settings
from services.llm import llm_service
from state import Incident, NeedType, RescueAssignment, RescueOutput, RunState, Urgency

URGENCY_WEIGHT = {"critical": 40, "high": 25, "medium": 10, "low": 3}
VULNERABLE_WEIGHT = {"elderly": 8, "children": 10, "disabled": 7, "infant": 12, "pregnant": 9, "medical_needs": 6}


def _credibility_map(state: RunState) -> Dict[str, float]:
    return {v.incident_id: v.credibility for v in state.verifications}


def compute_priority(incident: Incident, cred: float, now: datetime) -> tuple[float, List[str]]:
    """Priority formula: life risk, vulnerable people, time waiting, accessibility."""
    factors: List[str] = []
    score = 0.0

    u_weight = URGENCY_WEIGHT.get(incident.urgency.value, 10)
    score += u_weight
    factors.append(f"Urgency ({incident.urgency.value}): +{u_weight}")

    if incident.need_type in (NeedType.RESCUE, NeedType.EVACUATION):
        score += 20
        factors.append("Life-safety need type: +20")

    meta = incident.raw_metadata or {}
    vulnerable = meta.get("vulnerable", [])
    if isinstance(vulnerable, list):
        for tag in vulnerable:
            w = VULNERABLE_WEIGHT.get(tag, 3)
            score += w
            factors.append(f"Vulnerable ({tag}): +{w}")

    hours_waiting = max(0, (now - incident.timestamp.replace(tzinfo=None)).total_seconds() / 3600)
    wait_bonus = min(15, hours_waiting * 5)
    score += wait_bonus
    if wait_bonus > 0:
        factors.append(f"Time waiting ({hours_waiting:.1f}h): +{wait_bonus:.0f}")

    score *= cred
    factors.append(f"Credibility multiplier ({cred:.2f})")

    return score, factors


async def _explain(incident: Incident, score: float, factors: List[str]) -> str:
    if get_settings().effective_mock_mode:
        return f"Priority {score:.1f}: " + "; ".join(factors[:3])
    system = "Explain rescue priority in one sentence."
    user = f"Incident: {incident.text[:100]}. Score {score:.1f}. Factors: {factors}"
    try:
        return await llm_service.complete(system, user, max_tokens=80)
    except Exception:
        return f"Priority {score:.1f}: " + "; ".join(factors[:3])


async def run_rescue(state: RunState) -> RescueOutput:
    """Build ranked rescue queue for rescue/evacuation incidents."""
    cred_map = _credibility_map(state)
    now = datetime.utcnow()
    candidates = [
        i
        for i in state.incidents
        if i.need_type in (NeedType.RESCUE, NeedType.EVACUATION)
        and cred_map.get(i.id, 0) >= 0.35
    ]

    scored: List[tuple[Incident, float, List[str]]] = []
    for inc in candidates:
        c = cred_map.get(inc.id, 0.5)
        score, factors = compute_priority(inc, c, now)
        scored.append((inc, score, factors))

    scored.sort(key=lambda x: -x[1])
    queue: List[RescueAssignment] = []
    for rank, (inc, score, factors) in enumerate(scored, start=1):
        explanation = await _explain(inc, score, factors)
        vulnerable = inc.raw_metadata.get("vulnerable", []) if inc.raw_metadata else []
        queue.append(
            RescueAssignment(
                incident_id=inc.id,
                priority_score=round(score, 2),
                rank=rank,
                explanation=explanation,
                vulnerable_groups=list(vulnerable) if isinstance(vulnerable, list) else [],
            )
        )
    return RescueOutput(queue=queue)
