export * from '../types';
import type { Incident, ResponsePlan, RouteInfo, SimEvent, SimTeam, ActivityEvent, AlertDraft, HospitalAssignment, Zone } from '../types';

export type DisruptionKind = 'road_blocked' | 'bridge_collapsed' | 'site_inaccessible' | 'vehicle_failed' | 'region_hazard';

export interface Disruption {
  id: string;
  kind: DisruptionKind;
  /** road/bridge geometry as [lat,lng][] */
  geometry?: [number, number][];
  latlng?: [number, number];
  target_id?: string;
  severity: 'low' | 'medium' | 'high' | 'critical';
  status: 'active' | 'cleared';
  created_at: string;
  note?: string;
}

export type HazardType = 'flood' | 'fire' | 'collapse';

/**
 * Lifecycle of a report inside the simulation.
 * open = no team yet (deadline running) · assigned = a team is assigned, en route or on scene
 * resolved = the on-scene resolution timer finished · expired = no team arrived before the deadline.
 */
export type IncidentStatus = 'open' | 'assigned' | 'resolved' | 'expired';

export interface IncidentStatusEntry {
  status: IncidentStatus;
  /** sim-seconds at which it became resolved/expired (drives the fade-out); absent while still live */
  ended_at_s?: number;
}

export interface AffectedRegion {
  id: string;
  type: HazardType;
  name: string;
  center: [number, number];
  radius_m: number;
  severity: 'low' | 'medium' | 'high' | 'critical';
  growing: boolean;
  /** current outline as [lat,lng][] (grows over time) */
  ring: [number, number][];
}

/** How a position was obtained. `estimated` = dead-reckoned from elapsed time; `reported` = real GPS / check-in / sim. */
export type PositionSource = 'estimated' | 'reported';

/** A tracked position older than this (ms) is flagged stale on maps and in the tracker. */
export const STALE_AFTER_MS = 30_000;

export interface WorldVehicle extends SimTeam {
  failed?: 'stopped' | 'delayed' | null;
  needs_replan?: boolean;
  /** default 'reported' when absent */
  position_source?: PositionSource;
  /** epoch ms of the last real update for this vehicle; falls back to the world's `ts` */
  last_update_ts?: number;
}

export interface ExecutionItem {
  item_id: string;
  title: string;
  kind: 'rescue' | 'medical' | 'logistics';
  vehicle_id?: string;
  status: 'queued' | 'en_route' | 'on_site' | 'done' | 'blocked' | 'failed';
  eta_s: number;
  progress: number; // 0..1
  problem?: string;
  position_source?: PositionSource;
  last_update_ts?: number;
}

/** Everything the map draws. Published by the sim, received by every client. */
export type HospitalStatus = 'open' | 'full' | 'offline';

/** A patient who keeps an incident-assigned bed; walk-ins are only counted (see HospitalLoad). */
export interface HospitalPatient {
  id: string;
  incident_id: string;
  admitted_at_s: number;
  /** length of stay in sim-minutes */
  los_min: number;
  discharge_at_s: number;
}

/** One hospital's census. Every patient, walk-in or incident, has a length of stay; the bed frees when it ends. */
export interface HospitalLoad {
  capacity: number;
  occupied: number;
  free: number;
  /** occupied / capacity, 0..1 */
  occupancy: number;
  status: HospitalStatus;
  /** walk-in demand relative to a normal day (1 = normal): rises with disaster severity and nearness to hazards */
  demand_multiplier: number;
  /** patients in a bed right now, by origin */
  walk_in_patients: number;
  incident_patients: number;
  surge_patients: number;
  /** walk-ins who came from this hospital's catchment, wherever they ended up (here, diverted, or overflow) */
  arrivals_total: number;
  /** walk-ins admitted here (includes ones diverted in from a full neighbour) */
  walk_ins_total: number;
  discharged_total: number;
  /** patients this hospital sent elsewhere / took from elsewhere because it was full or offline */
  diverted_out: number;
  diverted_in: number;
  /** patients that no hospital could take */
  overflow: number;
  /** sim seconds until the next bed is freed; null when nobody is in a bed */
  next_discharge_s: number | null;
  /** incident patients currently in a bed */
  patients: HospitalPatient[];
}

export interface WorldState {
  /** run this world belongs to (set by the sim); lets every client follow the active run */
  run_id?: string | null;
  vehicles: WorldVehicle[];
  disruptions: Disruption[];
  affected_regions: AffectedRegion[];
  sim_incidents: Incident[];
  execution: ExecutionItem[];
  events: SimEvent[];
  beds: Record<string, number>;
  /** per-hospital availability; absent = all open */
  hospital_status?: Record<string, HospitalStatus>;
  /** per-hospital census (occupancy, stays, overflow); absent from older senders */
  hospital_load?: Record<string, HospitalLoad>;
  /** every incident patient admitted so far (in a bed or already discharged) */
  admitted_incident_ids?: string[];
  /** urgency raised by escalation, keyed by incident id */
  urgency_override: Record<string, Incident['urgency']>;
  /** lifecycle per tracked incident id; absent id = still open */
  incident_status?: Record<string, IncidentStatusEntry>;
  sim_time_s: number;
  ts: number;
}

export const EMPTY_WORLD: WorldState = {
  vehicles: [], disruptions: [], affected_regions: [], sim_incidents: [], execution: [], events: [], beds: {}, urgency_override: {}, sim_time_s: 0, ts: 0,
};

export type SimEventKind =
  | 'vehicle_failed' | 'route_blocked' | 'bridge_collapsed' | 'site_inaccessible'
  | 'incident_added' | 'incident_escalated' | 'region_added' | 'region_expanded' | 'fault_cleared'
  | 'hospital_full' | 'patient_diverted';

export interface SimBusEvent {
  kind: SimEventKind;
  text: string;
  at: string;
  disruption_id?: string;
  incident_id?: string;
  urgency?: Incident['urgency'];
}

/** Plan/run payload pushed on `plan.updated` / `plan.approved`. */
export interface PlanPayload {
  run_id: string;
  plan: ResponsePlan | null;
  routes: RouteInfo[];
  hospital_assignments: HospitalAssignment[];
  incidents: Incident[];
  zones: Zone[];
  alerts: AlertDraft[];
  activity_log: ActivityEvent[];
}


/** Citizen report as delivered by GET /reports and SSE `report.new`. */
export interface CitizenReport {
  token: string;
  created_at: string;
  submission: import('../types').ReportSubmission;
  /** run the backend attached this report to; absent = real report not tied to a sim run */
  run_id?: string | null;
}

export type ReplanAction = 'reroute' | 'hold';

/** `plan.replan` payload / POST /plan/{run}/replan response. The Route Agent's answer to a reroute request. */
export interface ReplanResult {
  run_id?: string;
  item_id: string;
  action: ReplanAction;
  ok?: boolean;
  /** rerouted: `route` starts at the vehicle's position. no_clean_detour: nothing hazard-free exists. */
  status?: 'rerouted' | 'no_clean_detour' | 'no_vehicle' | 'no_route' | 'unsupported';
  route?: RouteInfo | null;
  message?: string;
  reason?: string;
}
