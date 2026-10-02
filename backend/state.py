"""Pydantic models for shared state and agent contracts."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field


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


class ScenarioStartRequest(BaseModel):
    replay_speed: float = Field(default=1.0, ge=0.1, le=10.0)
    simulate_failures: List[str] = Field(default_factory=list)
