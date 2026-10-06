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
  /** ids of the signals that back / contradict this incident (absent from older backends) */
  supported_by?: string[];
  contradicted_by?: string[];
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

/**
 * External evidence in the common signal format (GET /signals): SACHET official alerts, Open-Meteo weather
 * (rain / river flood / wind / thunderstorm / heat) and GDELT news. No citizen data.
 */
export type SignalSeverity = 'unknown' | 'minor' | 'moderate' | 'severe' | 'extreme';
export type SignalSource = 'sachet' | 'gdelt' | 'open_meteo';

export interface Signal {
  id: string; // "sachet:<cap identifier>" | "open_meteo:<city>:<type>" | "gdelt:<hash of link>"
  source: SignalSource;
  kind: 'official_alert' | 'news' | 'weather';
  title: string;
  text: string;
  event: string;
  hazards: string[];
  severity: SignalSeverity;
  urgency: string;
  certainty: string;
  instruction: string;
  area: string;
  lat: number | null;
  lng: number | null;
  /** [south, west, north, east] */
  bbox: number[] | null;
  /** rings of [lat, lng] */
  polygons: number[][][];
  /** [lat, lng, radius_km] */
  circles: number[][];
  sender: string;
  source_url: string;
  language: string;
  msg_type: 'alert' | 'update' | 'cancel';
  /** ids of signals this one updates / cancels */
  references: string[];
  issued_at: string | null;
  effective_at: string | null;
  expires_at: string | null;
  fetched_at: string;
  status: 'active' | 'cancelled' | 'superseded';
  /** how far the source is trusted, and the ceiling of `weight` (SACHET 0.9, Open-Meteo 0.7, GDELT 0.5 by default) */
  trust: number;
  weight: number;
  /**
   * Small source-specific extras. Open-Meteo: city, endpoint, type, peak values. GDELT: domain, geo ('query_city'),
   * corroborating_domains. SACHET: category, response_type.
   */
  metadata: Record<string, unknown>;
  /** still in force (active and not expired) */
  active: boolean;
  /** 0..1, decays with age */
  freshness: number;
  /** current evidence strength, weight x freshness */
  score: number;
}

/** Result of POST /signals/<source>/refresh and the `last_refresh` entries of GET /signals/status. */
export interface SignalRefreshSummary {
  ok: boolean;
  disabled?: boolean;
  added?: number;
  updated?: number;
  expired?: number;
  pruned?: number;
  signals?: number;
  errors?: string[];
  at?: string;
  [key: string]: unknown;
}

export interface SignalSourceStatus {
  enabled: boolean;
  poll_seconds: number;
  last_refresh: SignalRefreshSummary | null;
  feeds?: string[];
  cities?: string[];
}

/** GET /signals/status */
export interface SignalsStatus {
  counts: Partial<Record<SignalSource, number>>;
  sachet: SignalSourceStatus;
  open_meteo: SignalSourceStatus;
  gdelt: SignalSourceStatus;
}

/** GET /signals/scoring: the constants in force (defaults plus environment overrides). */
export interface SignalScoring {
  source_trust: Record<SignalSource, number>;
  severity_weights: Record<string, number>;
  certainty_weights: Record<string, number>;
  urgency_weights: Record<string, number>;
  component_weights: Record<string, number>;
  freshness_half_life_hours: number;
  freshness_floor: number;
  open_meteo: {
    thresholds: Record<'rain_24h_mm' | 'rain_1h_mm' | 'flood_ratios' | 'wind_gust_kmh' | 'heat_c', Record<string, number>>;
    ttl_hours: number;
    certain_within_hours: number;
    flood: { enabled: boolean; past_days: number; horizon_days: number; min_discharge_m3s: number };
  };
  gdelt: {
    query_terms: string[];
    timespan: string;
    max_records: number;
    source_lang: string;
    ttl_hours: number;
    corroboration_domains: number;
    min_interval_seconds: number;
  };
}
