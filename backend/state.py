"""Pydantic models for shared state and agent contracts."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field, field_validator


class NeedType(str, Enum):
    RESCUE = "rescue"
    MEDICAL = "medical"
    SHELTER = "shelter"
    SUPPLIES = "supplies"
    EVACUATION = "evacuation"
    INFORMATION = "information"
    OTHER = "other"


class Urgency(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class Severity(str, Enum):
    LOW = "Low"
    MEDIUM = "Medium"
    HIGH = "High"
    CRITICAL = "Critical"


class IncidentSource(str, Enum):
    CITIZEN = "citizen"
    WEATHER = "weather"
    NEWS = "news"
    SYSTEM = "system"
    SIM = "sim"  # created by hand through POST /incidents (e.g. the simulator's Add Incident form)


class Incident(BaseModel):
    id: str
    text: str
    location: str
    lat: float
    lng: float
    need_type: NeedType
    urgency: Urgency
    source: IncidentSource
    timestamp: datetime
    raw_metadata: Dict[str, Any] = Field(default_factory=dict)


class VerificationResult(BaseModel):
    incident_id: str
    credibility: float = Field(ge=0.0, le=1.0)
    reasons: List[str]
    flagged: bool = False
    # Independent citizen reports about the same event nearby, this one included (copy-pasted reports count once).
    # NOT capped at the corroboration limit: kept separately so severity / priority can use it.
    crowd_size: int = Field(default=1, ge=1)
    # Ids of the external signals (SACHET / news / weather) that support this incident.
    supported_by: List[str] = Field(default_factory=list)
    # Ids of the external signals that CONTRADICT this incident (all clear / false alarm, normal conditions).
    contradicted_by: List[str] = Field(default_factory=list)


class Zone(BaseModel):
    id: str
    name: str
    center_lat: float
    center_lng: float
    severity: Severity
    incident_ids: List[str]
    summary: str = ""


class RescueAssignment(BaseModel):
    incident_id: str
    priority_score: float
    rank: int
    explanation: str
    vulnerable_groups: List[str] = Field(default_factory=list)


class HospitalAssignment(BaseModel):
    incident_id: str
    hospital_id: str
    hospital_name: str
    distance_km: float
    explanation: str
    specialty_match: bool = True
    # Set when the nearest hospital had no free beds and the patient was sent to the next nearest.
    diverted_from: Optional[str] = None
    # Set when every operational hospital is full: assigned to the nearest one anyway, needs escalation.
    overflow: bool = False
    # How long the patient is expected to occupy the bed, in simulated minutes. The simulator keeps the
    # bed taken for this long, then frees it (discharge).
    expected_stay_min: Optional[float] = None


class SupplyAllocation(BaseModel):
    zone_id: str
    warehouse_id: str
    warehouse_name: str
    items: Dict[str, int]
    shortage_flags: List[str] = Field(default_factory=list)
    explanation: str = ""


class RouteInfo(BaseModel):
    assignment_id: str
    assignment_type: Literal["rescue", "medical", "logistics"]
    from_lat: float
    from_lng: float
    to_lat: float
    to_lng: float
    distance_km: float
    duration_min: float
    geometry: List[List[float]] = Field(default_factory=list)
    blocked_warning: bool = False
    explanation: str = ""


class AlertDraft(BaseModel):
    audience: Literal["public", "field_teams", "hospitals"]
    title: str
    body: str


class PlanItemStatus(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EDITED = "edited"


class PlanItem(BaseModel):
    id: str
    category: Literal["rescue", "medical", "logistics", "route", "communication"]
    title: str
    description: str
    reasoning: str
    status: PlanItemStatus = PlanItemStatus.PENDING
    edited_content: Optional[str] = None
    incomplete: bool = False


class ResponsePlan(BaseModel):
    run_id: str
    rescue_queue: List[RescueAssignment] = Field(default_factory=list)
    hospital_assignments: List[HospitalAssignment] = Field(default_factory=list)
    supply_allocations: List[SupplyAllocation] = Field(default_factory=list)
    routes: List[RouteInfo] = Field(default_factory=list)
    items: List[PlanItem] = Field(default_factory=list)
    is_final: bool = False
    approved_at: Optional[datetime] = None


class AgentStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"


class ActivityEvent(BaseModel):
    run_id: str
    agent: str
    status: AgentStatus
    summary: str
    duration_ms: Optional[int] = None
    timestamp: datetime = Field(default_factory=datetime.utcnow)


class RunStatus(str, Enum):
    CREATED = "created"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class RunState(BaseModel):
    id: str
    status: RunStatus = RunStatus.CREATED
    created_at: datetime = Field(default_factory=datetime.utcnow)
    completed_at: Optional[datetime] = None
    incidents: List[Incident] = Field(default_factory=list)
    verifications: List[VerificationResult] = Field(default_factory=list)
    zones: List[Zone] = Field(default_factory=list)
    rescue_queue: List[RescueAssignment] = Field(default_factory=list)
    hospital_assignments: List[HospitalAssignment] = Field(default_factory=list)
    supply_allocations: List[SupplyAllocation] = Field(default_factory=list)
    routes: List[RouteInfo] = Field(default_factory=list)
    alerts: List[AlertDraft] = Field(default_factory=list)
    plan: Optional[ResponsePlan] = None
    activity_log: List[ActivityEvent] = Field(default_factory=list)
    agent_outputs: Dict[str, Any] = Field(default_factory=dict)
    incomplete_sections: List[str] = Field(default_factory=list)
    revision_count: int = 0
    simulate_failures: List[str] = Field(default_factory=list)
    # City (slug, e.g. "mumbai") this run plans for: hospitals, depots, flood zones, routing, weather.
    # None = the default city.
    city: Optional[str] = None
    # True when the Simulation app started this run. "Reset simulation" removes such runs and nothing else.
    simulated: bool = False


# Agent I/O contracts
class DetectionOutput(BaseModel):
    incidents: List[Incident]


class VerificationOutput(BaseModel):
    results: List[VerificationResult]


class DamageAssessmentOutput(BaseModel):
    zones: List[Zone]


class RescueOutput(BaseModel):
    queue: List[RescueAssignment]


class MedicalOutput(BaseModel):
    assignments: List[HospitalAssignment]


class LogisticsOutput(BaseModel):
    allocations: List[SupplyAllocation]


class RouteOutput(BaseModel):
    routes: List[RouteInfo]
    needs_revision: bool = False
    revision_reason: str = ""


class CommunicationOutput(BaseModel):
    alerts: List[AlertDraft]


class ReviewAction(BaseModel):
    item_id: str
    action: Literal["approve", "reject", "edit"]
    edited_content: Optional[str] = None


class ReviewRequest(BaseModel):
    actions: List[ReviewAction]


class ReplanRequest(BaseModel):
    item_id: str = Field(min_length=1, max_length=200)
    action: Literal["reroute", "hold"]


class IncidentCreate(BaseModel):
    """Body of POST /incidents: a manually created incident."""

    text: str = Field(min_length=1, max_length=2000)
    lat: float = Field(ge=-90, le=90)
    lng: float = Field(ge=-180, le=180)
    location: Optional[str] = Field(default=None, max_length=300)
    need_type: NeedType = NeedType.RESCUE
    urgency: Urgency = Urgency.MEDIUM
    people: Optional[int] = Field(default=None, ge=1, le=100000)
    vulnerable: List[str] = Field(default_factory=list, max_length=10)
    # Run to add the incident to. Omitted: the active run (a new run is started if there is none).
    run_id: Optional[str] = Field(default=None, max_length=200)
    # City for a new run, used only when the incident has to start one. An existing run keeps its own city.
    city: Optional[str] = Field(default=None, max_length=200)

    @field_validator("text")
    @classmethod
    def _strip_text(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("text must not be blank")
        return v


class ScenarioStartRequest(BaseModel):
    replay_speed: float = Field(default=1.0, ge=0.1, le=10.0)
    simulate_failures: List[str] = Field(default_factory=list)
    # City to plan for (name or slug). Its layers are loaded/fetched first. Omitted: the default city.
    city: Optional[str] = Field(default=None, max_length=200)
    # Sent by the Simulation app: the run is tagged as simulation-created so "Reset simulation" can remove it.
    simulation: bool = False
