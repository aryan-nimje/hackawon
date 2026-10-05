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
  /** name of the nearer hospital that was full; set when the patient was diverted */
  diverted_from?: string | null;
  /** every operational hospital was full, so this is the nearest one as overflow */
  overflow?: boolean;
  /** expected length of stay in sim-minutes; the bed stays taken that long */
  expected_stay_min?: number | null;
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

/* ───────── Citizen reports (contract for POST /reports, GET /reports/{token}) ───────── */

export type VulnerableGroup = 'children' | 'elderly' | 'disabled' | 'pregnant' | 'medical_needs';
export type HelpType = 'rescue' | 'medical' | 'shelter' | 'supplies' | 'other';
export type ReportStatus = 'received' | 'verifying' | 'prioritized' | 'assigned' | 'resolved';

export interface ReportSubmission {
  text: string;
  /** Where help is needed (pin position). */
  lat: number;
  lng: number;
  /** Browser-reported accuracy radius in metres, null if the pin was placed by hand. */
  accuracy_m: number | null;
  location_text: string;
  need_type: HelpType | null;
  vulnerable: VulnerableGroup[];
  people_count: number | null;
  /** false = reporting for someone else; the pin is *their* location. */
  is_own_location: boolean;
  /** Reporter's own GPS fix when different from the pin. */
  reporter_lat: number | null;
  reporter_lng: number | null;
  contact: string | null;
  /** Anti-abuse honeypot; must be empty. */
  website: string;
}

export interface ReportReceipt {
  token: string;
  status: ReportStatus;
  created_at: string;
}

export interface ReportStatusView {
  token: string;
  status: ReportStatus;
  updated_at: string;
  history: { status: ReportStatus; at: string }[];
  /** Citizen-safe summary of their own report only. */
  summary: string;
  note?: string | null;
}

/* ───────── Simulation (contract for /sim/*; a local engine implements it until the backend does) ───────── */

export interface SimTeam {
  id: string;
  kind: 'rescue' | 'medical' | 'logistics';
  lat: number;
  lng: number;
  status: 'en_route' | 'on_scene' | 'returning' | 'idle';
  target_id: string;
  eta_s: number;
}

export interface SimScore {
  /** share of junk reports (spam/duplicates/retractions) the system flagged */
  junk_caught: number;
  junk_total: number;
  /** genuine reports wrongly flagged */
  false_flags: number;
  genuine_total: number;
  /** share of truly critical incidents that got a rescue/medical team */
  critical_served: number;
  critical_total: number;
  /** mean sim-seconds from report to team dispatch */
  avg_response_s: number | null;
  /** assignments that crossed a flooded road at dispatch time */
  unsafe_routes: number;
  hospital_overflow: number;
  /** incident lifecycle counts (junk and feed items excluded); open = not yet resolved or expired */
  incidents_open: number;
  incidents_resolved: number;
  incidents_expired: number;
}

export interface SimEvent {
  at_s: number;
  kind: 'info' | 'flood' | 'dispatch' | 'arrive' | 'bed' | 'citizen' | 'warn';
  text: string;
}
