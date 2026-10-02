export type NeedType =
  | 'rescue'
  | 'medical'
  | 'shelter'
  | 'supplies'
  | 'evacuation'
  | 'information'
  | 'other';

export type Urgency = 'low' | 'medium' | 'high' | 'critical';
export type Severity = 'Low' | 'Medium' | 'High' | 'Critical';
export type PlanItemStatus = 'pending' | 'approved' | 'rejected' | 'edited';
export type AgentStatus = 'pending' | 'running' | 'completed' | 'failed' | 'skipped';
export type RunStatus = 'created' | 'running' | 'completed' | 'failed';

export interface Incident {
  id: string;
  text: string;
  location: string;
  lat: number;
  lng: number;
  need_type: NeedType;
  urgency: Urgency;
  source: string;
  timestamp: string;
  raw_metadata?: Record<string, unknown>;
  verification?: VerificationResult | null;
}

export interface VerificationResult {
  incident_id: string;
  credibility: number;
  reasons: string[];
  flagged: boolean;
}

export interface Zone {
  id: string;
  name: string;
  center_lat: number;
  center_lng: number;
  severity: Severity;
  incident_ids: string[];
  summary: string;
}

export interface RescueAssignment {
  incident_id: string;
  priority_score: number;
  rank: number;
  explanation: string;
  vulnerable_groups: string[];
}

export interface HospitalAssignment {
  incident_id: string;
  hospital_id: string;
  hospital_name: string;
  distance_km: number;
  explanation: string;
  specialty_match: boolean;
}

export interface SupplyAllocation {
  zone_id: string;
  warehouse_id: string;
  warehouse_name: string;
  items: Record<string, number>;
  shortage_flags: string[];
  explanation: string;
}

export interface RouteInfo {
  assignment_id: string;
  assignment_type: 'rescue' | 'medical' | 'logistics';
  from_lat: number;
  from_lng: number;
  to_lat: number;
  to_lng: number;
  distance_km: number;
  duration_min: number;
  geometry: number[][];
  blocked_warning: boolean;
  explanation: string;
}

export interface PlanItem {
  id: string;
  category: 'rescue' | 'medical' | 'logistics' | 'route' | 'communication';
  title: string;
  description: string;
  reasoning: string;
  status: PlanItemStatus;
  edited_content?: string | null;
  incomplete: boolean;
}

export interface ResponsePlan {
  run_id: string;
  rescue_queue: RescueAssignment[];
  hospital_assignments: HospitalAssignment[];
  supply_allocations: SupplyAllocation[];
  routes: RouteInfo[];
  items: PlanItem[];
  is_final: boolean;
  approved_at?: string | null;
}

export interface AlertDraft {
  audience: 'public' | 'field_teams' | 'hospitals';
  title: string;
  body: string;
}

export interface ActivityEvent {
  run_id: string;
  agent: string;
  status: AgentStatus;
  summary: string;
  duration_ms?: number | null;
  timestamp: string;
}

export interface RunState {
  id: string;
  status: RunStatus;
  created_at: string;
  completed_at?: string | null;
  incidents: Incident[];
  verifications: VerificationResult[];
  zones: Zone[];
  rescue_queue: RescueAssignment[];
  hospital_assignments: HospitalAssignment[];
  supply_allocations: SupplyAllocation[];
  routes: RouteInfo[];
  alerts: AlertDraft[];
  plan?: ResponsePlan | null;
  activity_log: ActivityEvent[];
  incomplete_sections: string[];
}

export interface HealthResponse {
  status: string;
  mock_mode: boolean;
  llm_provider: string;
}

export interface BlockedZoneFeature {
  type: 'Feature';
  properties: { name: string; reason: string };
  geometry: { type: 'Polygon'; coordinates: number[][][] };
}
