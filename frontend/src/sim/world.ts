/**
 * Client-side simulated world (read-write). The backend supplies decisions (incidents, plan,
 * routes); this world supplies consequences: vehicles, faults, growing regions, escalation.
 * Everything is in sim-seconds. Mutating tool methods push an undo snapshot first.
 */
import {
  BASE_FLOOD_ZONES, HOSPITALS, circleRing, haversineKm, pointInRing, scaleRing,
} from '../lib/layers';
import { BRIDGES } from '../data/bridges';
import { countIncidentStatuses, isJunkIncident } from '../shared/incidents';
import type {
  AffectedRegion, Disruption, ExecutionItem, HazardType, HospitalAssignment, Incident, IncidentStatus, IncidentStatusEntry, PlanItem,
  HospitalStatus, ReplanResult, RouteInfo, SimBusEvent, SimEvent, SimScore, Transfer, WorldState, WorldVehicle,
} from '../shared/types';
import type { Preset } from './presets';
import { HospitalLedger, SIM_MIN, sampleAcuity, sampleStayS, type Hazard, type LedgerState } from './hospitals';

const MAX_GROWTH = 2.2;
const GROWTH_PER_S = 0.012;
const TRAVEL_COMPRESSION = 0.25;
/**
 * Incident timing. Durations are authored in "real seconds at the normal 5x speed" and converted to
 * sim-seconds once, so the world only ever compares sim-time to sim-time. Sim-time itself advances by
 * (elapsed real time x current speed) in the tick loop, so a speed change only changes how fast the
 * timers progress from now on; it never rescales time already elapsed (no jumps), and pausing the
 * loop freezes every timer.
 *   8 real min  = 40 sim-min  : a team must be assigned by then or the incident expires
 *   3-5 real min = 15-25 sim-min: random on-scene resolution time once a team arrives / patient is admitted
 */
const NORMAL_SPEED = 5;
const ASSIGN_DEADLINE_S = 8 * 60 * NORMAL_SPEED;
const RESOLVE_MIN_S = 3 * 60 * NORMAL_SPEED;
const RESOLVE_MAX_S = 5 * 60 * NORMAL_SPEED;
/**
 * Hospitals: every patient has a length of stay (see hospitals.ts) and keeps a bed until it ends. Walk-in demand
 * rises with hazard severity and nearness. Diversions of walk-ins are announced at most once per hospital per
 * this many sim-seconds (a surge can divert dozens of patients a minute), and a census line is logged this often.
 */
const DIVERT_ANNOUNCE_EVERY_S = 5 * SIM_MIN;
const CENSUS_LOG_EVERY_S = 10 * SIM_MIN;
/** A hospital is "filling up" from this share of beds occupied. */
const FILLING_AT = 0.85;
const BLOCK_RADIUS_KM = 0.16;
/** A route that starts inside a blockage's radius is "leaving" it only if it never gets this much closer (mirrors the Route Agent). */
const LEAVE_MARGIN_KM = 0.05;
const URGENCY_ORDER: Incident['urgency'][] = ['low', 'medium', 'high', 'critical'];

interface Region {
  id: string; type: HazardType; name: string; reason: string;
  baseRing: [number, number][]; center: [number, number]; severity: AffectedRegion['severity'];
  growing: boolean; startedAt: number; base: boolean;
}

type Halt = null | 'manual' | 'route' | 'site';
interface TeamRt {
  id: string; itemId: string; kind: WorldVehicle['kind']; targetId: string; route: RouteInfo;
  pts: number[][]; elapsed: number; travelS: number; arrivedAt: number | null; riskWarned: boolean;
  halted: Halt; delayUntil: number | null; vanished: boolean; ignored: Set<string>;
  /** a re-route request is with the Route Agent */
  rerouting: boolean;
  /** the Route Agent found no hazard-free route; the vehicle waits for Authority */
  noDetour: boolean;
  /** earliest sim time an automatic re-route may be requested again (after a failed request) */
  nextAutoAt: number;
}

/** A diverted patient driving from the full hospital to the one that took them. Gone the moment it arrives. */
interface TransferRt {
  id: string; incidentId: string; fromName: string; toName: string;
  geom: number[][]; pts: number[][]; elapsed: number; travelS: number;
  /** false until the road route is known (or the request has failed) */
  ready: boolean; createdT: number;
}

/** Direction of travel (degrees clockwise from north) at fraction f along a path. */
export function headingAt(pts: number[][], f: number): number | undefined {
  if (pts.length < 2) return undefined;
  const i = Math.floor(Math.min(1, Math.max(0, f)) * (pts.length - 1));
  let a = pts[Math.max(0, Math.min(i, pts.length - 3))];
  let b = pts[Math.min(pts.length - 1, Math.max(i, 0) + 3)];
  if (a[0] === b[0] && a[1] === b[1]) { a = pts[0]; b = pts[pts.length - 1]; }
  const dLat = b[0] - a[0];
  const dLng = (b[1] - a[1]) * Math.cos((a[0] * Math.PI) / 180);
  if (dLat === 0 && dLng === 0) return undefined;
  return Math.round(((Math.atan2(dLng, dLat) * 180) / Math.PI + 360) % 360);
}

export interface WorldInputs {
  incidents: Incident[];
  planItems: PlanItem[];
  routes: RouteInfo[];
  hospitalAssignments: HospitalAssignment[];
  autoReplan: boolean;
  escalateAfterS: number;
}

export interface WorldSnapshot extends WorldState {
  t: number;
  dispatchedItemIds: Set<string>;
  firstSeen: Map<string, number>;
  responseTimes: number[];
  unsafeRoutes: number;
  overflow: number;
  diverted: number;
  canUndo: boolean;
}

export function isFeed(inc: Incident): boolean {
  return inc.source === 'weather' || inc.source === 'news' || inc.source === 'system';
}
export function isJunk(inc: Incident): boolean {
  return isJunkIncident(inc);
}
export function isTrueCritical(inc: Incident): boolean {
  return !isFeed(inc) && !isJunk(inc) && inc.urgency === 'critical' && (inc.need_type === 'rescue' || inc.need_type === 'medical');
}

export function pointAlong(geometry: number[][], f: number): [number, number] {
  if (geometry.length === 0) return [0, 0];
  if (geometry.length === 1 || f <= 0) return [geometry[0][0], geometry[0][1]];
  if (f >= 1) { const l = geometry[geometry.length - 1]; return [l[0], l[1]]; }
  const seg: number[] = [];
  let total = 0;
  for (let i = 1; i < geometry.length; i++) {
    const d = Math.hypot(geometry[i][0] - geometry[i - 1][0], geometry[i][1] - geometry[i - 1][1]);
    seg.push(d); total += d;
  }
  let target = f * total;
  for (let i = 0; i < seg.length; i++) {
    if (target <= seg[i] || i === seg.length - 1) {
      const r = seg[i] === 0 ? 0 : target / seg[i];
      return [geometry[i][0] + (geometry[i + 1][0] - geometry[i][0]) * r, geometry[i][1] + (geometry[i + 1][1] - geometry[i][1]) * r];
    }
    target -= seg[i];
  }
  return [geometry[0][0], geometry[0][1]];
}

/** Insert points so no segment is longer than ~maxKm (route geometry may be a 2-point line). */
function densify(g: number[][], maxKm = 0.05): number[][] {
  const out: number[][] = [];
  for (let i = 0; i < g.length; i++) {
    out.push(g[i]);
    if (i === g.length - 1) break;
    const d = haversineKm(g[i][0], g[i][1], g[i + 1][0], g[i + 1][1]);
    const n = Math.min(400, Math.floor(d / maxKm));
    for (let k = 1; k <= n; k++) {
      const f = k / (n + 1);
      out.push([g[i][0] + (g[i + 1][0] - g[i][0]) * f, g[i][1] + (g[i + 1][1] - g[i][1]) * f]);
    }
  }
  return out;
}

interface UndoState {
  disruptions: Disruption[];
  regions: Region[];
  simIncidents: Incident[];
  teamFaults: Record<string, { halted: Halt; delayUntil: number | null; vanished: boolean; ignored: string[] }>;
  ledger: LedgerState;
  hospitalStatus: Record<string, HospitalStatus>;
}

export class SimWorld {
  t = 0;
  private regions: Region[] = [];
  private disruptions: Disruption[] = [];
  private simIncidents: Incident[] = [];
  private ledger: HospitalLedger;
  private hospitalStatus: Record<string, HospitalStatus>;
  private fullNotified = new Set<string>();
  /** walk-in diversions / overflow since the last announcement, per hospital */
  private divertTally = new Map<string, { diverted: number; overflow: number; lastAt: number }>();
  private filling = new Set<string>();
  private lastCensusLog = 0;
  private diverted = 0;
  private teams: TeamRt[] = [];
  private doneItems = new Set<string>();
  private skipped = new Map<string, string>();
  private lastApproved: PlanItem[] = [];
  private doneTeams = 0;
  private events: SimEvent[] = [];
  private outbox: SimBusEvent[] = [];
  private firstSeen = new Map<string, number>();
  private lastLevelAt = new Map<string, number>();
  private override: Record<string, Incident['urgency']> = {};
  private dispatched = new Set<string>();
  private served = new Set<string>();
  private responseTimes: number[] = [];
  private unsafe = 0;
  private overflow = 0;
  private floodedHospitals = new Set<string>();
  private autoBlocks = new Set<string>();
  private lastAuto = 0;
  private counter = 0;
  private undoStack: UndoState[] = [];
  private rerouteQueue: string[] = [];
  private transfers: TransferRt[] = [];
  private transferQueue: { id: string; from: [number, number]; to: [number, number] }[] = [];
  /** lifecycle of every tracked incident (absent = open, never seen as terminal) */
  private incState = new Map<string, IncidentStatusEntry>();
  /** target id (incident or zone) -> sim time at which on-scene work finishes; set when the first team arrives */
  private resolveAt = new Map<string, number>();

  private rand: () => number;

  /** `rand` is injectable so tests can pin the random resolution time. */
  constructor(rand: () => number = Math.random) {
    this.rand = rand;
    this.regions = BASE_FLOOD_ZONES.map((z) => ({
      id: z.id, type: 'flood', name: z.name, reason: z.reason, baseRing: z.ring,
      center: [z.ring.reduce((s, p) => s + p[0], 0) / z.ring.length, z.ring.reduce((s, p) => s + p[1], 0) / z.ring.length],
      severity: 'medium', growing: true, startedAt: 0, base: true,
    }));
    this.ledger = new HospitalLedger(HOSPITALS, rand);
    this.ledger.seedNormalDay(0);
    this.hospitalStatus = Object.fromEntries(HOSPITALS.map((h) => [h.id, 'open' as HospitalStatus]));
    this.log('info', 'Simulation world initialised. Waiting for the scenario.');
  }

  /* ───────── helpers ───────── */
  private log(kind: SimEvent['kind'], text: string) {
    this.events.push({ at_s: Math.round(this.t), kind, text });
    if (this.events.length > 300) this.events.splice(0, this.events.length - 300);
  }
  private emit(e: Omit<SimBusEvent, 'at'>) {
    this.outbox.push({ ...e, at: new Date().toISOString() });
  }
  drainEvents(): SimBusEvent[] {
    const o = this.outbox;
    this.outbox = [];
    return o;
  }
  /** Plan item ids whose vehicle wants a new route from the Route Agent (POST /plan/{run}/replan). */
  /** Hospital-to-hospital transfers that still need a road route from the backend. */
  drainTransferRequests() {
    const q = this.transferQueue;
    this.transferQueue = [];
    return q;
  }

  /** The road route for a transfer arrived: the ambulance starts driving along it. */
  setTransferRoute(id: string, geometry: number[][], durationMin: number) {
    const tr = this.transfers.find((x) => x.id === id);
    if (!tr || tr.ready) return;
    if (geometry.length >= 2) { tr.geom = geometry; tr.pts = densify(geometry); }
    tr.travelS = Math.max(15, durationMin * 60 * TRAVEL_COMPRESSION);
    tr.ready = true;
  }

  /** No road route available: drive the straight line instead. */
  transferRouteFailed(id: string) {
    const tr = this.transfers.find((x) => x.id === id);
    if (!tr || tr.ready) return;
    const a = tr.geom[0];
    const b = tr.geom[tr.geom.length - 1];
    tr.travelS = Math.max(15, (haversineKm(a[0], a[1], b[0], b[1]) / 40) * 3600 * TRAVEL_COMPRESSION);
    tr.ready = true;
  }

  private startTransfer(incidentId: string, from: { id: string; name: string; lat: number; lng: number }, to: { id: string; name: string; lat: number; lng: number }) {
    const id = this.nextId('transfer');
    const geom = [[from.lat, from.lng], [to.lat, to.lng]];
    this.transfers.push({ id, incidentId, fromName: from.name, toName: to.name, geom, pts: densify(geom), elapsed: 0, travelS: 60, ready: false, createdT: this.t });
    this.transferQueue.push({ id, from: [from.lat, from.lng], to: [to.lat, to.lng] });
  }

  drainRerouteRequests(): string[] {
    const q = this.rerouteQueue;
    this.rerouteQueue = [];
    return q;
  }
  /** The re-route request itself failed (network/backend down): stay halted and retry later. */
  rerouteFailed(itemId: string) {
    const tm = this.teams.find((x) => x.itemId === itemId);
    if (!tm) return;
    tm.rerouting = false;
    tm.nextAutoAt = this.t + 15;
    this.log('warn', `${tm.id}: could not reach the Route Agent for a new route.`);
  }
  private requestReroute(tm: TeamRt) {
    tm.rerouting = true;
    this.rerouteQueue.push(tm.itemId);
    this.log('dispatch', `${tm.id} asked the Route Agent for a new route from its current position.`);
  }
  private nextId(p: string) { this.counter += 1; return `${p}-${this.counter}`; }

  private growth(r: Region): number {
    return r.growing ? Math.min(MAX_GROWTH, 1 + GROWTH_PER_S * Math.max(0, this.t - r.startedAt)) : 1;
  }
  private ring(r: Region) { return scaleRing(r.baseRing, this.growth(r)); }

  private pushUndo() {
    const faults: UndoState['teamFaults'] = {};
    for (const tm of this.teams) faults[tm.id] = { halted: tm.halted, delayUntil: tm.delayUntil, vanished: tm.vanished, ignored: [...tm.ignored] };
    this.undoStack.push({
      disruptions: structuredClone(this.disruptions),
      regions: structuredClone(this.regions),
      simIncidents: structuredClone(this.simIncidents),
      teamFaults: faults,
      ledger: this.ledger.save(),
      hospitalStatus: { ...this.hospitalStatus },
    });
    if (this.undoStack.length > 30) this.undoStack.shift();
  }

  undo() {
    const u = this.undoStack.pop();
    if (!u) return;
    this.disruptions = u.disruptions;
    this.regions = u.regions;
    this.simIncidents = u.simIncidents;
    this.ledger.restore(u.ledger);
    this.hospitalStatus = u.hospitalStatus;
    for (const tm of this.teams) {
      const f = u.teamFaults[tm.id];
      if (f) { tm.halted = f.halted; tm.delayUntil = f.delayUntil; tm.vanished = f.vanished; tm.ignored = new Set(f.ignored); }
    }
    this.log('info', 'Last change undone.');
  }

  /* ───────── tools ───────── */
  addRegion(type: HazardType, lat: number, lng: number, radiusM: number, severity: AffectedRegion['severity'], growing: boolean) {
    this.pushUndo();
    const id = this.nextId(`region`);
    this.regions.push({
      id, type, name: `${type[0].toUpperCase()}${type.slice(1)} zone`, reason: 'Reported hazard',
      baseRing: circleRing(lat, lng, Math.max(80, radiusM)), center: [lat, lng], severity, growing, startedAt: this.t, base: false,
    });
    this.log('flood', `${type} region added at ${lat.toFixed(4)}, ${lng.toFixed(4)}${growing ? ' (growing)' : ''}.`);
    this.emit({ kind: 'region_added', text: `New ${type} region (${severity}) added${growing ? ', growing' : ''}.` });
  }

  removeRegion(id: string) {
    const r = this.regions.find((x) => x.id === id);
    if (!r || r.base) return;
    this.pushUndo();
    this.regions = this.regions.filter((x) => x.id !== id);
    this.log('info', `Region ${r.name} removed.`);
    this.emit({ kind: 'fault_cleared', text: `${r.name} removed.` });
  }

  blockRoad(lat: number, lng: number) {
    this.pushUndo();
    const d: Disruption = {
      id: this.nextId('road'), kind: 'road_blocked', latlng: [lat, lng],
      severity: 'critical', status: 'active', created_at: new Date().toISOString(), note: 'Road blocked',
    };
    this.disruptions.push(d);
    this.log('warn', `Road blocked near ${lat.toFixed(4)}, ${lng.toFixed(4)}.`);
    this.emit({ kind: 'route_blocked', text: `Road blocked near ${lat.toFixed(4)}, ${lng.toFixed(4)}.`, disruption_id: d.id });
  }

  toggleBridge(bridgeId: string) {
    const b = BRIDGES.find((x) => x.id === bridgeId);
    if (!b) return;
    this.pushUndo();
    const existing = this.disruptions.find((d) => d.kind === 'bridge_collapsed' && d.target_id === bridgeId);
    if (existing) {
      this.disruptions = this.disruptions.filter((d) => d !== existing);
      this.log('info', `${b.name} restored.`);
      this.emit({ kind: 'fault_cleared', text: `${b.name} restored.`, disruption_id: existing.id });
      return;
    }
    const d: Disruption = {
      id: this.nextId('bridge'), kind: 'bridge_collapsed', geometry: b.geometry, target_id: bridgeId,
      severity: 'critical', status: 'active', created_at: new Date().toISOString(), note: `${b.name} collapsed`,
    };
    this.disruptions.push(d);
    this.log('warn', `${b.name} has collapsed.`);
    this.emit({ kind: 'bridge_collapsed', text: `${b.name} has collapsed.`, disruption_id: d.id });
  }

  toggleSite(targetId: string, lat: number, lng: number, label: string) {
    this.pushUndo();
    const existing = this.disruptions.find((d) => d.kind === 'site_inaccessible' && d.target_id === targetId);
    if (existing) {
      this.disruptions = this.disruptions.filter((d) => d !== existing);
      this.log('info', `Entry to ${label} restored.`);
      this.emit({ kind: 'fault_cleared', text: `Entry to ${label} restored.`, disruption_id: existing.id });
      return;
    }
    const d: Disruption = {
      id: this.nextId('site'), kind: 'site_inaccessible', latlng: [lat, lng], target_id: targetId,
      severity: 'high', status: 'active', created_at: new Date().toISOString(), note: `Entry to ${label} not possible`,
    };
    this.disruptions.push(d);
    this.log('warn', `Entry to ${label} is not possible.`);
    this.emit({ kind: 'site_inaccessible', text: `Entry to ${label} is not possible.`, disruption_id: d.id, incident_id: targetId });
  }

  clearDisruption(id: string) {
    const d = this.disruptions.find((x) => x.id === id);
    if (!d) return;
    this.pushUndo();
    this.disruptions = this.disruptions.filter((x) => x.id !== id);
    this.log('info', `Cleared: ${d.note ?? d.kind}.`);
    this.emit({ kind: 'fault_cleared', text: `Cleared: ${d.note ?? d.kind}.`, disruption_id: id });
  }

  failVehicle(id: string, mode: 'stop' | 'vanish' | 'delay', delayS = 120) {
    const tm = this.teams.find((x) => x.id === id);
    if (!tm) return;
    this.pushUndo();
    if (mode === 'vanish') {
      tm.vanished = true;
      this.disruptions.push({
        id: this.nextId('veh'), kind: 'vehicle_failed', latlng: this.posOf(tm), target_id: tm.id, severity: 'critical',
        status: 'active', created_at: new Date().toISOString(), note: `${tm.id} lost`,
      });
      this.log('warn', `${tm.id} (${tm.kind}) lost contact / destroyed.`);
      this.emit({ kind: 'vehicle_failed', text: `${tm.id} (${tm.kind}) destroyed or lost.` });
      return;
    }
    tm.halted = 'manual';
    tm.delayUntil = mode === 'delay' ? this.t + delayS : null;
    this.disruptions.push({
      id: this.nextId('veh'), kind: 'vehicle_failed', latlng: this.posOf(tm), target_id: tm.id,
      severity: 'high', status: 'active', created_at: new Date().toISOString(),
      note: mode === 'delay' ? `${tm.id} delayed ${delayS}s` : `${tm.id} broke down`,
    });
    this.log('warn', mode === 'delay' ? `${tm.id} delayed for ${delayS}s.` : `${tm.id} broke down on the road.`);
    this.emit({ kind: 'vehicle_failed', text: mode === 'delay' ? `${tm.id} delayed ${delayS}s.` : `${tm.id} broke down.` });
  }

  restoreVehicle(id: string) {
    const tm = this.teams.find((x) => x.id === id);
    if (!tm) return;
    this.pushUndo();
    tm.halted = null; tm.delayUntil = null; tm.vanished = false;
    this.disruptions = this.disruptions.filter((d) => !(d.kind === 'vehicle_failed' && d.target_id === id));
    this.log('info', `${id} restored and moving again.`);
    this.emit({ kind: 'fault_cleared', text: `${id} restored.` });
  }

  /** A manual incident was created on the backend (POST /incidents). The backend owns it; this only logs it. */
  noteIncidentAdded(inc: Pick<Incident, 'id' | 'urgency' | 'need_type'>) {
    this.log('info', `Incident ${inc.id} added: ${inc.urgency} ${inc.need_type}.`);
    this.emit({ kind: 'incident_added', text: `Incident ${inc.id} added (${inc.urgency} ${inc.need_type}).`, incident_id: inc.id });
  }

  loadPreset(p: Preset) {
    this.pushUndo();
    for (const r of p.regions) {
      const id = this.nextId('region');
      this.regions.push({ id, type: r.type, name: `${r.type} zone (preset ${p.name})`, reason: `Preset: ${p.name}`, baseRing: circleRing(r.center[0], r.center[1], r.radius_m), center: r.center, severity: r.severity, growing: r.growing, startedAt: this.t, base: false });
    }
    for (const i of p.incidents) {
      this.simIncidents.push({ id: this.nextId('sim'), ...i, source: 'sim', timestamp: new Date().toISOString(), raw_metadata: { preset: p.id }, verification: null });
    }
    for (const r of p.roads) {
      this.disruptions.push({ id: this.nextId('road'), kind: 'road_blocked', latlng: r.latlng, severity: 'critical', status: 'active', created_at: new Date().toISOString(), note: 'Road blocked' });
    }
    for (const b of p.bridges) {
      const br = BRIDGES.find((x) => x.id === b);
      if (br && !this.disruptions.some((d) => d.target_id === b)) {
        this.disruptions.push({ id: this.nextId('bridge'), kind: 'bridge_collapsed', geometry: br.geometry, target_id: b, severity: 'critical', status: 'active', created_at: new Date().toISOString(), note: `${br.name} collapsed` });
      }
    }
    this.log('info', `Preset "${p.name}" loaded.`);
    this.emit({ kind: 'region_added', text: `Scenario preset "${p.name}" loaded.` });
  }

  clearFaults() {
    this.pushUndo();
    this.disruptions = [];
    this.regions = this.regions.filter((r) => r.base);
    this.simIncidents = [];
    this.override = {};
    this.autoBlocks.clear();
    for (const tm of this.teams) { tm.halted = null; tm.delayUntil = null; tm.vanished = false; tm.ignored.clear(); }
    this.log('info', 'All faults cleared.');
    this.emit({ kind: 'fault_cleared', text: 'All faults cleared.' });
  }

  /* ───────── hospital controls ───────── */
  private hospital(id: string) { return HOSPITALS.find((h) => h.id === id); }

  /** Beds free right now, per hospital. Derived from the ledger, never stored separately. */
  private freeBeds(): Record<string, number> {
    return Object.fromEntries(HOSPITALS.map((h) => [h.id, this.ledger.free(h.id)]));
  }

  /** A hospital can take a patient only when open and it has a free bed. */
  private hasRoom(id: string): boolean {
    return this.hospitalStatus[id] === 'open' && this.ledger.free(id) > 0;
  }

  /** Set the number of free beds (clamped to 0..capacity): patients are held on the ward or discharged early. Reopens a hospital that was forced full. */
  setBeds(id: string, free: number) {
    const h = this.hospital(id);
    if (!h) return;
    this.pushUndo();
    const n = Math.max(0, Math.min(h.beds, Math.round(free)));
    const cur = this.ledger.free(id);
    if (n < cur) this.ledger.occupyBeds(id, cur - n, this.t);
    else if (n > cur) this.ledger.releaseBeds(id, n - cur);
    if (this.hospitalStatus[id] === 'full' && n > 0) this.hospitalStatus[id] = 'open';
    this.log('bed', `${h.name}: beds set to ${this.ledger.free(id)}/${h.beds} free by operator.`);
    if (this.ledger.free(id) > 0) this.fullNotified.delete(id);
    else this.notifyFull(id, `${h.name} has no free beds.`);
  }

  /** Mark full (every bed held, new patients diverted until reopened) or reopen with a few beds free. Patients still leave when their stay ends. */
  markFull(id: string, full: boolean) {
    const h = this.hospital(id);
    if (!h || this.hospitalStatus[id] === 'offline') return;
    this.pushUndo();
    if (full) {
      this.hospitalStatus[id] = 'full';
      this.ledger.occupyBeds(id, this.ledger.free(id), this.t);
      this.log('warn', `${h.name} marked full.`);
      this.notifyFull(id, `${h.name} is full. New patients are diverted.`);
    } else {
      this.hospitalStatus[id] = 'open';
      const want = Math.ceil(h.beds * 0.2);
      if (this.ledger.free(id) < want) this.ledger.releaseBeds(id, want - this.ledger.free(id));
      this.fullNotified.delete(id);
      this.log('info', `${h.name} reopened with ${this.ledger.free(id)} beds.`);
      this.emit({ kind: 'fault_cleared', text: `${h.name} reopened for admissions.` });
    }
  }

  setOffline(id: string, offline: boolean) {
    const h = this.hospital(id);
    if (!h) return;
    this.pushUndo();
    if (offline) {
      this.hospitalStatus[id] = 'offline';
      this.log('warn', `${h.name} taken offline.`);
      this.notifyFull(id, `${h.name} is offline. No patients can be admitted.`);
    } else {
      this.hospitalStatus[id] = 'open';
      this.fullNotified.delete(id);
      this.log('info', `${h.name} back online.`);
      this.emit({ kind: 'fault_cleared', text: `${h.name} back online.` });
    }
  }

  private notifyFull(id: string, text: string) {
    if (this.fullNotified.has(id)) return;
    this.fullNotified.add(id);
    this.emit({ kind: 'hospital_full', text });
  }

  /** Nearest hospital (from a point) that can still take a patient, skipping flooded ones and `exclude`. */
  private nextHospital(from: [number, number], exclude: string): typeof HOSPITALS[number] | null {
    let best: typeof HOSPITALS[number] | null = null;
    let bd = Infinity;
    for (const h of HOSPITALS) {
      if (h.id === exclude || !this.hasRoom(h.id) || this.floodedHospitals.has(h.id)) continue;
      const d = haversineKm(from[0], from[1], h.lat, h.lng);
      if (d < bd) { bd = d; best = h; }
    }
    return best;
  }

  /**
   * The Route Agent's answer to a re-route request (or Authority's hold). A new route starts at the
   * vehicle's current position, so the vehicle restarts along the new road geometry with the
   * route's own OSRM time. No clean route: the vehicle stays put and waits for Authority.
   */
  applyReplan(res: ReplanResult) {
    const tm = this.teams.find((x) => x.itemId === res.item_id);
    if (!tm || res.action === 'hold') return;
    tm.rerouting = false;
    if (tm.halted === 'site') {
      // Entry to the site itself is the problem, not the road: Authority lets the team go in.
      for (const d of this.activeBlockers(tm, tm.elapsed / tm.travelS)) tm.ignored.add(d.id);
      tm.travelS = Math.max(tm.travelS, tm.elapsed + 30);
      tm.halted = null;
      this.log('dispatch', `${tm.id} cleared to enter on authority approval.`);
      return;
    }
    if (tm.halted !== 'route') return;
    const route = res.route;
    if (res.status === 'rerouted' && route && route.geometry.length >= 2) {
      tm.route = route;
      tm.pts = densify(route.geometry);
      tm.travelS = Math.max(20, route.duration_min * 60 * TRAVEL_COMPRESSION);
      tm.elapsed = 0;
      tm.ignored.clear();
      tm.riskWarned = false;
      tm.noDetour = false;
      tm.halted = null;
      this.log('dispatch', `${tm.id} re-routed: ${route.distance_km} km, ~${route.duration_min} min on a clean route from its position.`);
      return;
    }
    if (res.status === 'no_clean_detour') {
      tm.noDetour = true;
      this.log('warn', `${tm.id}: ${res.message || 'no clean detour found'}. Waiting for Authority.`);
      this.emit({ kind: 'route_blocked', text: `${tm.id}: no clean detour found.`, incident_id: tm.targetId });
      return;
    }
    tm.nextAutoAt = this.t + 15;
    this.log('warn', `${tm.id}: re-route not applied (${res.status ?? 'unknown'}${res.message ? `: ${res.message}` : ''}).`);
  }

  private posOf(tm: TeamRt): [number, number] {
    return pointAlong(tm.pts, tm.arrivedAt == null ? Math.min(1, tm.elapsed / tm.travelS) : 1);
  }

  /* ───────── blockers ───────── */
  private activeBlockers(tm: TeamRt, f: number): Disruption[] {
    const start = Math.floor(Math.min(1, Math.max(0, f)) * (tm.pts.length - 1));
    const ends = [tm.pts[0], tm.pts[tm.pts.length - 1]];
    const out: Disruption[] = [];
    for (const d of this.disruptions) {
      if (d.status !== 'active' || tm.ignored.has(d.id)) continue;
      if (d.kind !== 'road_blocked' && d.kind !== 'bridge_collapsed') continue;
      const refs: [number, number][] = d.geometry ?? (d.latlng ? [d.latlng] : []);
      // A hazard only blocks a route whose endpoints are both outside it: going into one, or leaving one, is allowed.
      const nearest = (p: [number, number]) => refs.reduce((m, r) => Math.min(m, haversineKm(p[0], p[1], r[0], r[1])), Infinity);
      if (nearest(ends[1]) <= BLOCK_RADIUS_KM) continue; // going into it
      const startD = nearest(ends[0]);
      if (startD <= BLOCK_RADIUS_KM) {
        // A team stopped AT the blockage is inside its radius but may only LEAVE it (turn round). A route that drives on
        // towards the blockage is still blocked, otherwise the new route would send it straight back through.
        const closest = tm.pts.reduce((m, p) => Math.min(m, nearest(p)), Infinity);
        if (closest >= startD - LEAVE_MARGIN_KM) continue;
      }
      let hit = false;
      for (let j = start; j < tm.pts.length && !hit; j++) {
        for (const r of refs) {
          if (haversineKm(tm.pts[j][0], tm.pts[j][1], r[0], r[1]) <= BLOCK_RADIUS_KM) { hit = true; break; }
        }
      }
      if (hit) out.push(d);
    }
    return out;
  }

  private siteBlocked(targetId: string): boolean {
    return this.disruptions.some((d) => d.kind === 'site_inaccessible' && d.target_id === targetId && d.status === 'active');
  }

  /* ───────── incident lifecycle ───────── */
  private statusOf(id: string): IncidentStatus { return this.incState.get(id)?.status ?? 'open'; }

  private setStatus(id: string, status: IncidentStatus) {
    const prev = this.incState.get(id);
    if (prev?.status === status) return;
    this.incState.set(id, status === 'resolved' || status === 'expired' ? { status, ended_at_s: this.t } : { status });
  }

  /** A team is assigned to this incident and still alive (en route, on scene, or temporarily halted). */
  private hasLiveTeam(id: string): boolean {
    return this.teams.some((tm) => tm.targetId === id && tm.kind !== 'logistics' && !tm.vanished);
  }

  /** Start the random on-scene timer for a target the first time a team reaches it / a patient is admitted. Idempotent. */
  private startResolution(targetId: string) {
    if (this.resolveAt.has(targetId)) return;
    const dur = RESOLVE_MIN_S + this.rand() * (RESOLVE_MAX_S - RESOLVE_MIN_S);
    this.resolveAt.set(targetId, this.t + dur);
    this.log('info', `${targetId}: resolution started, ~${Math.round(dur / 60)} sim-min on scene.`);
  }

  /** Resolve incidents whose timer finished; expire unattended ones past the deadline. Teams with an assignment never expire. */
  private updateIncidentLifecycle(allIncidents: Incident[]) {
    for (const inc of allIncidents) {
      if (isFeed(inc) || isJunk(inc)) continue;
      const cur = this.statusOf(inc.id);
      if (cur === 'resolved' || cur === 'expired') continue;
      const due = this.resolveAt.get(inc.id);
      if (due != null && this.t >= due) {
        this.setStatus(inc.id, 'resolved');
        this.log('arrive', `${inc.id} resolved.`);
      } else if (this.hasLiveTeam(inc.id) || due != null) {
        this.setStatus(inc.id, 'assigned');
      } else {
        const seen = this.firstSeen.get(inc.id) ?? this.t;
        if (this.t - seen >= ASSIGN_DEADLINE_S) {
          this.setStatus(inc.id, 'expired');
          this.log('warn', `${inc.id} expired: no team was assigned within ${Math.round(ASSIGN_DEADLINE_S / 60)} sim-min.`);
        } else this.setStatus(inc.id, 'open');
      }
    }
  }

  /* ───────── tick ───────── */
  /** `dt` is sim-seconds: elapsed real time already multiplied by the current speed. */
  tick(dt: number, input: WorldInputs) {
    this.t += dt;
    const zones = this.regions.map((r) => ({ id: r.id, ring: this.ring(r) }));
    this.lastApproved = input.planItems.filter((i) => i.status === 'approved' || i.status === 'edited');
    const allIncidents = [...input.incidents, ...this.simIncidents];

    // 1. new reports
    for (const inc of allIncidents) {
      if (isFeed(inc) || this.firstSeen.has(inc.id)) continue;
      this.firstSeen.set(inc.id, this.t);
      const fromCitizen = inc.source === 'citizen';
      if (fromCitizen || inc.urgency === 'critical' || inc.urgency === 'high') {
        this.log(fromCitizen ? 'citizen' : 'info', `${fromCitizen ? 'Citizen report' : 'Report'} ${inc.id}: ${inc.urgency} ${inc.need_type} at ${inc.location}`);
      }
    }

    // 2. dispatch approved items
    for (const item of input.planItems) {
      if (this.dispatched.has(item.id)) continue;
      if (item.status !== 'approved' && item.status !== 'edited') continue;
      const dash = item.id.indexOf('-');
      if (dash < 0) continue;
      const kind = item.id.slice(0, dash);
      const targetId = item.id.slice(dash + 1);
      if (kind !== 'rescue' && kind !== 'medical' && kind !== 'logistics') continue;
      const route = input.routes.find((r) => r.assignment_type === kind && r.assignment_id === targetId);
      this.dispatched.add(item.id);
      if (kind !== 'logistics' && (this.statusOf(targetId) === 'expired' || this.statusOf(targetId) === 'resolved')) {
        const why = this.statusOf(targetId) === 'expired' ? 'Incident expired before a team was assigned' : 'Incident already resolved';
        this.skipped.set(item.id, why);
        this.log('warn', `Approved ${item.title} not dispatched: ${why.toLowerCase()}.`);
        continue;
      }
      if (!route || route.geometry.length < 2) {
        this.skipped.set(item.id, 'No usable route');
        this.log('warn', `Approved ${item.title} has no usable route; dispatch skipped.`);
        continue;
      }
      const crossesNow = this.routeCrossesFlood(route, zones);
      if (crossesNow || route.blocked_warning) {
        this.unsafe += 1;
        this.log('warn', `${item.title}: dispatched on a route that crosses a flooded zone.`);
      }
      const seen = this.firstSeen.get(targetId);
      if (seen != null && kind !== 'logistics') this.responseTimes.push(this.t - seen);
      this.served.add(targetId);
      const travelS = Math.max(20, route.duration_min * 60 * TRAVEL_COMPRESSION);
      const id = `T${this.teams.length + this.doneTeams + 1}`;
      this.teams.push({
        id, itemId: item.id, kind, targetId, route, pts: densify(route.geometry), elapsed: 0, travelS, arrivedAt: null,
        riskWarned: crossesNow, halted: null, delayUntil: null, vanished: false, ignored: new Set(),
        rerouting: false, noDetour: false, nextAutoAt: 0,
      });
      this.log('dispatch', `${id} (${kind}) dispatched to ${targetId}, ETA ${Math.round(travelS)}s`);
    }

    // 3. auto-block roads inside growing regions (every ~5 sim-s)
    if (this.t - this.lastAuto >= 5) {
      this.lastAuto = this.t;
      for (const tm of this.teams) {
        if (tm.arrivedAt != null || tm.vanished) continue;
        const start = Math.floor(Math.min(1, tm.elapsed / tm.travelS) * (tm.pts.length - 1));
        for (const z of zones) {
          const reg = this.regions.find((r) => r.id === z.id);
          if (!reg || !reg.growing) continue;
          const key = `${z.id}:${tm.id}`;
          if (this.autoBlocks.has(key)) continue;
          // Rising water only cuts a route whose endpoints are both outside it: rescues going in and teams leaving are never blocked.
          const first = tm.pts[0];
          const last = tm.pts[tm.pts.length - 1];
          if (pointInRing(first[0], first[1], z.ring) || pointInRing(last[0], last[1], z.ring)) continue;
          const hit = tm.pts.slice(start).find((p) => pointInRing(p[0], p[1], z.ring));
          if (hit) {
            this.autoBlocks.add(key);
            const d: Disruption = {
              id: this.nextId('road'), kind: 'road_blocked', latlng: [hit[0], hit[1]], severity: 'high', status: 'active',
              created_at: new Date().toISOString(), note: `Road blocked by ${reg.name}`,
            };
            this.disruptions.push(d);
            this.log('flood', `Rising water blocked a road under ${tm.id}'s route.`);
            this.emit({ kind: 'region_expanded', text: `${reg.name} expanded and blocked a road on ${tm.id}'s route.`, disruption_id: d.id });
          }
        }
      }
    }

    // 4. move teams
    for (const tm of this.teams) {
      if (tm.vanished) continue;
      if (tm.arrivedAt != null) continue;
      if (tm.halted === 'manual' && tm.delayUntil != null && this.t >= tm.delayUntil) {
        tm.halted = null; tm.delayUntil = null;
        this.disruptions = this.disruptions.filter((d) => !(d.kind === 'vehicle_failed' && d.target_id === tm.id));
        this.log('info', `${tm.id} delay is over, moving again.`);
      }
      if (tm.halted === 'route') {
        if (this.activeBlockers(tm, tm.elapsed / tm.travelS).length === 0) {
          tm.halted = null;
          tm.noDetour = false;
          tm.rerouting = false;
          this.log('info', `${tm.id}: route is clear again, resuming.`);
        } else if (input.autoReplan && !tm.rerouting && !tm.noDetour && this.t >= tm.nextAutoAt) {
          this.requestReroute(tm);
        }
        continue;
      }
      if (tm.halted === 'site') {
        if (!this.siteBlocked(tm.targetId)) { tm.halted = null; } else continue;
      }
      if (tm.halted === 'manual') continue;

      tm.elapsed += dt;
      const f = tm.elapsed / tm.travelS;
      const blockers = this.activeBlockers(tm, f);
      if (blockers.length) {
        tm.halted = 'route';
        if (input.autoReplan) {
          this.log('warn', `${tm.id} stopped: ${blockers[0].note ?? 'road blocked'}.`);
          this.requestReroute(tm);
        } else {
          this.log('warn', `${tm.id} stopped: ${blockers[0].note ?? 'road blocked'}. Awaiting authority re-plan.`);
          this.emit({ kind: 'route_blocked', text: `${tm.id} cannot continue: ${blockers[0].note ?? 'road blocked'}.`, disruption_id: blockers[0].id, incident_id: tm.targetId });
        }
        continue;
      }
      if (f >= 1) {
        if (this.siteBlocked(tm.targetId)) {
          tm.elapsed = tm.travelS;
          tm.halted = 'site';
          this.log('warn', `${tm.id} reached ${tm.targetId} but entry is not possible.`);
          this.emit({ kind: 'site_inaccessible', text: `${tm.id} cannot enter ${tm.targetId}.`, incident_id: tm.targetId });
          continue;
        }
        tm.arrivedAt = this.t;
        this.log('arrive', `${tm.id} reached ${tm.targetId}`);
        this.startResolution(tm.targetId);
        if (tm.kind === 'medical') this.admitPatient(tm.targetId, input.hospitalAssignments, this.posOf(tm));
      } else if (!tm.riskWarned && this.routeCrossesFlood(tm.route, zones)) {
        tm.riskWarned = true;
        this.unsafe += 1;
        this.log('warn', `Road flooded mid-route: ${tm.id}'s route to ${tm.targetId} now crosses a flood zone.`);
      }
    }
    // teams leave once the on-scene resolution timer for their target has run out
    const done = this.teams.filter((tm) => {
      const due = tm.arrivedAt != null ? this.resolveAt.get(tm.targetId) : undefined;
      return due != null && this.t >= due;
    });
    for (const d of done) this.doneItems.add(d.itemId);
    this.teams = this.teams.filter((tm) => !done.includes(tm));
    this.doneTeams += done.length;

    // 4a. diverted patients drive to the hospital that took them; the path disappears on arrival
    for (const tr of this.transfers) {
      if (!tr.ready) { if (this.t - tr.createdT > 60) this.transferRouteFailed(tr.id); continue; }
      tr.elapsed += dt;
    }
    const arrivedTransfers = this.transfers.filter((tr) => tr.ready && tr.elapsed >= tr.travelS);
    for (const tr of arrivedTransfers) this.log('arrive', `Patient from ${tr.incidentId} arrived at ${tr.toName}.`);
    if (arrivedTransfers.length) this.transfers = this.transfers.filter((tr) => !arrivedTransfers.includes(tr));

    // 4b. incident lifecycle: assigned / resolved / expired (after dispatch and arrival, so a team assigned this tick is never expired)
    this.updateIncidentLifecycle(allIncidents);

    // 5. escalation of unattended incidents
    for (const inc of allIncidents) {
      if (isFeed(inc) || isJunk(inc) || this.served.has(inc.id)) continue;
      const st = this.statusOf(inc.id);
      if (st === 'expired' || st === 'resolved') continue;
      const cur = this.override[inc.id] ?? inc.urgency;
      if (cur === 'critical') continue;
      const since = this.lastLevelAt.get(inc.id) ?? this.firstSeen.get(inc.id) ?? this.t;
      if (this.t - since >= input.escalateAfterS) {
        const next = URGENCY_ORDER[Math.min(3, URGENCY_ORDER.indexOf(cur) + 1)];
        this.override[inc.id] = next;
        this.lastLevelAt.set(inc.id, this.t);
        this.log('warn', `${inc.id} escalated to ${next} (unattended).`);
        this.emit({ kind: 'incident_escalated', text: `${inc.id} escalated to ${next}: no team assigned.`, incident_id: inc.id, urgency: next });
      }
    }

    // 6. hospitals: stays end (beds free), then walk-in demand arrives and is admitted, diverted or overflows
    this.updateHospitals(dt);
    for (const h of HOSPITALS) {
      if (this.floodedHospitals.has(h.id)) continue;
      if (zones.some((z) => pointInRing(h.lat, h.lng, z.ring))) {
        this.floodedHospitals.add(h.id);
        this.log('warn', `${h.name} is now inside an affected region. Access may be cut.`);
      }
    }
  }

  /** The road path crosses a zone while both route endpoints are outside it (into / out of a zone is not a crossing). */
  private routeCrossesFlood(route: RouteInfo, zones: { id: string; ring: [number, number][] }[]): boolean {
    const g = route.geometry;
    if (g.length < 2) return false;
    const a = g[0];
    const b = g[g.length - 1];
    const step = Math.max(1, Math.floor(g.length / 400));
    for (const z of zones) {
      if (pointInRing(a[0], a[1], z.ring) || pointInRing(b[0], b[1], z.ring)) continue;
      for (let i = 0; i < g.length; i += step) {
        if (pointInRing(g[i][0], g[i][1], z.ring)) return true;
      }
    }
    return false;
  }

  /**
   * An incident patient reaches the hospital the Medical Agent chose. They take a bed for the stay the agent
   * expects. If that hospital is full or offline they go to the nearest one with room; if none has room they overflow.
   */
  private admitPatient(incidentId: string, assignments: HospitalAssignment[], from: [number, number]) {
    const a = assignments.find((x) => x.incident_id === incidentId);
    if (!a) return;
    const stayS = a.expected_stay_min ? a.expected_stay_min * SIM_MIN : sampleStayS('major', this.rand);
    const take = (hospitalId: string) => this.ledger.admit(hospitalId, { source: 'incident', acuity: 'major', incident_id: incidentId, t: this.t, los_s: stayS });
    const stayText = `stays ~${Math.round(stayS / SIM_MIN)} sim-min`;
    if (this.hasRoom(a.hospital_id) && take(a.hospital_id)) {
      this.startResolution(incidentId);
      this.log('bed', `Patient from ${incidentId} admitted to ${a.hospital_name} (${stayText}); ${this.ledger.free(a.hospital_id)} beds left`);
      if (this.ledger.free(a.hospital_id) === 0) this.notifyFull(a.hospital_id, `${a.hospital_name} has no free beds left.`);
      return;
    }
    const why = this.hospitalStatus[a.hospital_id] === 'offline' ? 'offline' : 'full';
    const alt = this.nextHospital(from, a.hospital_id);
    if (alt && take(alt.id)) {
      this.startResolution(incidentId);
      this.diverted += 1;
      this.ledger.noteDiverted(a.hospital_id, alt.id);
      const origin = this.hospital(a.hospital_id);
      if (origin) this.startTransfer(incidentId, origin, alt);
      const km = haversineKm(from[0], from[1], alt.lat, alt.lng);
      this.log('bed', `${a.hospital_name} is ${why}: patient from ${incidentId} diverted to ${alt.name} (${km.toFixed(1)} km, ${stayText}), ${this.ledger.free(alt.id)} beds left`);
      this.emit({ kind: 'patient_diverted', text: `Patient from ${incidentId} diverted from ${a.hospital_name} (${why}) to ${alt.name}, ${km.toFixed(1)} km away.`, incident_id: incidentId });
      if (this.ledger.free(alt.id) === 0) this.notifyFull(alt.id, `${alt.name} has no free beds left.`);
    } else {
      this.overflow += 1;
      this.ledger.noteOverflow(a.hospital_id);
      this.log('warn', `${a.hospital_name} is ${why} and no other hospital has free beds: patient from ${incidentId} cannot be placed.`);
      this.notifyFull(a.hospital_id, `No hospital can take the patient from ${incidentId}.`);
    }
  }

  /** Hazard regions as demand sees them: centre, size now (they grow), severity. */
  private hazards(): Hazard[] {
    return this.regions.map((r) => {
      const ring = this.ring(r);
      const radiusKm = Math.max(...ring.map((p) => haversineKm(r.center[0], r.center[1], p[0], p[1])));
      return { lat: r.center[0], lng: r.center[1], radius_km: radiusKm, severity: r.severity, scale: this.growth(r) };
    });
  }

  /** Discharge patients whose stay ended, then place the walk-ins that arrived this tick. */
  private updateHospitals(dt: number) {
    // individual discharges are not logged (they are frequent); the census counts them
    for (const id of this.ledger.dischargeDue(this.t).keys()) {
      if (this.ledger.free(id) > 0) this.fullNotified.delete(id);
    }
    for (const a of this.ledger.sampleArrivals(dt, this.hazards())) this.placeWalkIn(a.hospital_id, a.acuity);
    this.announceHospitals();
  }

  /** A walk-in goes to the hospital whose catchment they are in; if it is full or offline, to the nearest with room; else overflow. */
  private placeWalkIn(originId: string, acuity: ReturnType<typeof sampleAcuity>) {
    const origin = this.hospital(originId);
    if (!origin) return;
    let target: typeof HOSPITALS[number] | null = this.hasRoom(origin.id) ? origin : null;
    if (!target) target = this.nextHospital([origin.lat, origin.lng], origin.id);
    const tally = this.divertTally.get(origin.id) ?? { diverted: 0, overflow: 0, lastAt: -Infinity };
    this.divertTally.set(origin.id, tally);
    if (target && this.ledger.admit(target.id, { source: 'walk_in', acuity, t: this.t })) {
      if (target.id !== origin.id) {
        this.diverted += 1;
        this.ledger.noteDiverted(origin.id, target.id);
        tally.diverted += 1;
      }
      if (this.ledger.free(target.id) === 0) this.notifyFull(target.id, `${target.name} has no free beds left.`);
    } else {
      this.overflow += 1;
      this.ledger.noteOverflow(origin.id);
      tally.overflow += 1;
      this.notifyFull(origin.id, `${origin.name} is full and no other hospital has free beds.`);
    }
  }

  /** Log and announce what matters: hospitals filling up, batches of diversions, overflow, and a periodic census. */
  private announceHospitals() {
    for (const h of HOSPITALS) {
      const occ = this.ledger.occupied(h.id) / h.beds;
      if (occ >= FILLING_AT && !this.filling.has(h.id)) {
        this.filling.add(h.id);
        this.log('bed', `${h.name} is filling up: ${this.ledger.occupied(h.id)}/${h.beds} beds occupied.`);
      } else if (occ < FILLING_AT - 0.1 && this.filling.has(h.id)) {
        this.filling.delete(h.id);
        this.log('bed', `${h.name} is recovering: ${this.ledger.free(h.id)} beds free as stays end.`);
      }
      const tally = this.divertTally.get(h.id);
      if (tally && (tally.diverted || tally.overflow) && this.t - tally.lastAt >= DIVERT_ANNOUNCE_EVERY_S) {
        if (tally.diverted) {
          this.log('bed', `${h.name}: ${tally.diverted} walk-in patient(s) diverted to other hospitals.`);
          this.emit({ kind: 'patient_diverted', text: `${tally.diverted} walk-in patient(s) diverted away from ${h.name} (no free beds).` });
        }
        if (tally.overflow) this.log('warn', `${h.name}: ${tally.overflow} walk-in patient(s) could not be placed anywhere.`);
        tally.diverted = 0; tally.overflow = 0; tally.lastAt = this.t;
      }
    }
    if (this.t - this.lastCensusLog >= CENSUS_LOG_EVERY_S) {
      this.lastCensusLog = this.t;
      const occ = this.ledger.totalOccupied();
      const cap = this.ledger.totalCapacity();
      this.log('bed', `Hospitals: ${occ}/${cap} beds occupied (${Math.round((100 * occ) / cap)}%).`);
    }
  }

  /* ───────── snapshot ───────── */
  private execution(): ExecutionItem[] {
    const out: ExecutionItem[] = [];
    for (const item of this.lastApproved) {
      const dash = item.id.indexOf('-');
      const kind = item.id.slice(0, dash) as ExecutionItem['kind'];
      if (kind !== 'rescue' && kind !== 'medical' && kind !== 'logistics') continue;
      const tm = this.teams.find((x) => x.itemId === item.id);
      const base = { item_id: item.id, title: item.title, kind, ...(tm ? { position_source: 'reported' as const, last_update_ts: Date.now() } : {}) };
      if (tm) {
        const progress = Math.min(1, tm.elapsed / tm.travelS);
        const eta = Math.max(0, Math.round(tm.travelS - tm.elapsed));
        if (tm.vanished) out.push({ ...base, vehicle_id: tm.id, status: 'failed', eta_s: 0, progress, problem: 'Vehicle destroyed or lost' });
        else if (tm.halted === 'manual') out.push({ ...base, vehicle_id: tm.id, status: 'blocked', eta_s: eta, progress, problem: tm.delayUntil ? 'Vehicle delayed' : 'Vehicle broke down' });
        else if (tm.halted === 'route') out.push({ ...base, vehicle_id: tm.id, status: 'blocked', eta_s: eta, progress, problem: tm.noDetour ? 'no clean detour found' : 'Road or bridge blocked on route' });
        else if (tm.halted === 'site') out.push({ ...base, vehicle_id: tm.id, status: 'blocked', eta_s: 0, progress: 1, problem: 'Site entry not possible' });
        else if (tm.arrivedAt != null) out.push({ ...base, vehicle_id: tm.id, status: 'on_site', eta_s: 0, progress: 1 });
        else out.push({ ...base, vehicle_id: tm.id, status: 'en_route', eta_s: eta, progress });
      } else if (this.doneItems.has(item.id)) out.push({ ...base, status: 'done', eta_s: 0, progress: 1 });
      else if (this.skipped.has(item.id)) out.push({ ...base, status: 'failed', eta_s: 0, progress: 0, problem: this.skipped.get(item.id) });
      else out.push({ ...base, status: 'queued', eta_s: 0, progress: 0 });
    }
    return out;
  }

  snapshot(): WorldSnapshot {
    const vehicles: WorldVehicle[] = this.teams.filter((tm) => !tm.vanished).map((tm) => {
      const f = tm.arrivedAt == null ? Math.min(1, tm.elapsed / tm.travelS) : 1;
      const [lat, lng] = pointAlong(tm.pts, f);
      return {
        id: tm.id, kind: tm.kind, lat, lng, status: tm.arrivedAt == null ? 'en_route' : 'on_scene', target_id: tm.targetId,
        eta_s: tm.arrivedAt == null ? Math.max(0, Math.round(tm.travelS * (1 - f))) : 0,
        heading: headingAt(tm.pts, f),
        failed: tm.halted === 'manual' ? (tm.delayUntil ? 'delayed' : 'stopped') : null,
        needs_replan: tm.halted === 'route' || tm.halted === 'site',
        // the simulator plays the role of the vehicle's GPS: every position it publishes is a real report
        position_source: 'reported', last_update_ts: Date.now(),
      } as WorldVehicle;
    });
    const transfers: Transfer[] = this.transfers.filter((tr) => tr.ready).map((tr) => {
      const f = Math.min(1, tr.elapsed / tr.travelS);
      const [lat, lng] = pointAlong(tr.pts, f);
      const i = Math.floor(f * (tr.pts.length - 1));
      return {
        id: tr.id, incident_id: tr.incidentId, from_name: tr.fromName, to_name: tr.toName, lat, lng,
        path: [[lat, lng], ...tr.pts.slice(i + 1)], eta_s: Math.max(0, Math.round(tr.travelS - tr.elapsed)),
      };
    });
    const regions: AffectedRegion[] = this.regions.map((r) => {
      const ring = this.ring(r);
      const rad = Math.max(...ring.map((p) => haversineKm(r.center[0], r.center[1], p[0], p[1]))) * 1000;
      return { id: r.id, type: r.type, name: r.name, center: r.center, radius_m: Math.round(rad), severity: r.severity, growing: r.growing, ring };
    });
    return {
      t: this.t, sim_time_s: this.t, ts: Date.now(),
      vehicles, transfers, disruptions: structuredClone(this.disruptions), affected_regions: regions,
      sim_incidents: structuredClone(this.simIncidents), execution: this.execution(), events: [...this.events],
      beds: this.freeBeds(), hospital_status: { ...this.hospitalStatus },
      hospital_load: this.ledger.census(this.t, this.hospitalStatus), admitted_incident_ids: this.ledger.admittedIncidentIds(),
      urgency_override: { ...this.override },
      incident_status: Object.fromEntries(this.incState),
      dispatchedItemIds: new Set(this.dispatched), firstSeen: new Map(this.firstSeen),
      responseTimes: [...this.responseTimes], unsafeRoutes: this.unsafe, overflow: this.overflow, diverted: this.diverted,
      canUndo: this.undoStack.length > 0,
    };
  }
}

export function computeScore(snap: WorldSnapshot, incidents: Incident[]): SimScore {
  const reports = incidents.filter((i) => !isFeed(i));
  const junk = reports.filter(isJunk);
  const genuine = reports.filter((i) => !isJunk(i));
  const flagged = (i: Incident) => !!i.verification?.flagged;
  const criticals = reports.filter(isTrueCritical);
  const served = criticals.filter((i) => ['rescue', 'medical'].some((k) => snap.dispatchedItemIds.has(`${k}-${i.id}`)));
  const counts = countIncidentStatuses(incidents, snap.incident_status);
  const avg = snap.responseTimes.length ? snap.responseTimes.reduce((a, b) => a + b, 0) / snap.responseTimes.length : null;
  return {
    junk_total: junk.length, junk_caught: junk.filter(flagged).length,
    false_flags: genuine.filter(flagged).length, genuine_total: genuine.length,
    critical_total: criticals.length, critical_served: served.length,
    avg_response_s: avg, unsafe_routes: snap.unsafeRoutes, hospital_overflow: snap.overflow,
    incidents_open: counts.open, incidents_resolved: counts.resolved, incidents_expired: counts.expired,
  };
}
