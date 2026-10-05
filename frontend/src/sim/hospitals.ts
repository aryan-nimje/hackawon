/**
 * Hospital patient ledger for the simulated world. Pure (no DOM, no network) so it can be tested alone.
 *
 * Every patient in a bed has a length of stay. The bed stays occupied until `discharge_at_s`; `dischargeDue`
 * then frees it. Occupancy is never stored separately: it is the number of patients in the ledger, so
 * admissions, discharges, operator changes and the published census cannot drift apart.
 *
 * Walk-in demand is a Poisson process per hospital. Its rate is the hospital's normal-day rate times a
 * multiplier that grows with the severity and size of every hazard region and with how close the hospital is
 * to it (see `demandMultiplier`).
 *
 * All times are sim-seconds. Lengths of stay are authored in sim-minutes (SIM_MIN seconds).
 */
import type { HospitalLoad, HospitalPatient } from '../shared/types';

export const SIM_MIN = 60;

export type PatientSource = 'walk_in' | 'incident' | 'surge';
export type Acuity = 'minor' | 'moderate' | 'major';
export type Severity = 'low' | 'medium' | 'high' | 'critical';

export interface Patient {
  id: string;
  source: PatientSource;
  acuity: Acuity;
  incident_id?: string;
  admitted_at_s: number;
  los_s: number;
  discharge_at_s: number;
}

export interface HospitalSpec { id: string; lat: number; lng: number; beds: number }

/** A hazard region as seen by demand: where it is, how big, how severe, how much it has grown. */
export interface Hazard { lat: number; lng: number; radius_km: number; severity: Severity; scale: number }

/** Length of stay by acuity, sim-minutes [min, max]. */
export const STAY_MIN: Record<Acuity, [number, number]> = {
  minor: [15, 35],
  moderate: [35, 70],
  major: [70, 120],
};
/** Mean stay of a normal-day walk-in mix (0.6 minor, 0.3 moderate, 0.1 major), used to size the normal arrival rate. */
const NORMAL_MEAN_STAY_MIN = 0.6 * 25 + 0.3 * 52.5 + 0.1 * 95;
/** Share of beds taken on a normal day. */
export const NORMAL_OCCUPANCY = 0.5;
/** Normal-day walk-ins per bed per sim-second, so that normal occupancy settles at NORMAL_OCCUPANCY. */
const NORMAL_ARRIVALS_PER_BED_S = NORMAL_OCCUPANCY / (NORMAL_MEAN_STAY_MIN * SIM_MIN);

export const SEVERITY_WEIGHT: Record<Severity, number> = { low: 0.5, medium: 1, high: 2, critical: 3 };
/** Distance from the edge of a hazard over which its local surge falls to 1/e. */
const NEAR_KM = 2.5;
/** City-wide demand added per unit of hazard weight (people across the city seek care for disaster injuries). */
const CITYWIDE_GAIN = 0.02;
/** Extra demand at a hospital right at a hazard, per unit of hazard weight. */
const LOCAL_GAIN = 0.22;
export const MAX_MULTIPLIER = 5;

function haversineKm(aLat: number, aLng: number, bLat: number, bLng: number): number {
  const r = 6371;
  const p1 = (aLat * Math.PI) / 180;
  const p2 = (bLat * Math.PI) / 180;
  const dp = p2 - p1;
  const dl = ((bLng - aLng) * Math.PI) / 180;
  const h = Math.sin(dp / 2) ** 2 + Math.cos(p1) * Math.cos(p2) * Math.sin(dl / 2) ** 2;
  return 2 * r * Math.asin(Math.sqrt(h));
}

/**
 * Walk-in demand relative to a normal day: 1 with no hazards. Each hazard adds a city-wide part and a local
 * part that decays with the distance from its edge, both scaled by its severity and growth.
 */
export function demandMultiplier(h: { lat: number; lng: number }, hazards: Hazard[]): number {
  let extra = 0;
  for (const z of hazards) {
    // casualties grow more slowly than the hazard's radius, hence the square root of its growth
    const w = SEVERITY_WEIGHT[z.severity] * Math.sqrt(Math.max(0, z.scale));
    const edgeKm = Math.max(0, haversineKm(h.lat, h.lng, z.lat, z.lng) - z.radius_km);
    extra += w * (CITYWIDE_GAIN + LOCAL_GAIN * Math.exp(-edgeKm / NEAR_KM));
  }
  return Math.min(MAX_MULTIPLIER, 1 + extra);
}

/** Poisson sample. Knuth for small means, normal approximation beyond that. */
export function poisson(mean: number, rand: () => number): number {
  if (mean <= 0) return 0;
  if (mean > 30) {
    const u1 = Math.max(1e-12, rand());
    const u2 = rand();
    return Math.max(0, Math.round(mean + Math.sqrt(mean) * Math.sqrt(-2 * Math.log(u1)) * Math.cos(2 * Math.PI * u2)));
  }
  const limit = Math.exp(-mean);
  let k = 0;
  let p = 1;
  do { k += 1; p *= rand(); } while (p > limit);
  return k - 1;
}

/** Disasters send sicker people: the acuity mix shifts towards major injuries as demand rises. */
export function sampleAcuity(multiplier: number, rand: () => number): Acuity {
  const surge = Math.max(0, multiplier - 1);
  const pMajor = Math.min(0.3, 0.1 + 0.04 * surge);
  const pModerate = Math.min(0.45, 0.3 + 0.03 * surge);
  const r = rand();
  if (r < pMajor) return 'major';
  if (r < pMajor + pModerate) return 'moderate';
  return 'minor';
}

export function sampleStayS(acuity: Acuity, rand: () => number): number {
  const [lo, hi] = STAY_MIN[acuity];
  return (lo + rand() * (hi - lo)) * SIM_MIN;
}

interface Counters {
  arrivals: number; walkIns: number; discharged: number; divertedOut: number; divertedIn: number; overflow: number; multiplier: number;
}

export interface LedgerState { patients: Record<string, Patient[]>; admittedIncidents: string[] }

export class HospitalLedger {
  private patients = new Map<string, Patient[]>();
  private counters = new Map<string, Counters>();
  private admittedIncidents = new Set<string>();
  private nextPatient = 0;

  private specs: HospitalSpec[];
  private rand: () => number;

  constructor(specs: HospitalSpec[], rand: () => number = Math.random) {
    this.specs = specs;
    this.rand = rand;
    for (const h of specs) {
      this.patients.set(h.id, []);
      this.counters.set(h.id, { arrivals: 0, walkIns: 0, discharged: 0, divertedOut: 0, divertedIn: 0, overflow: 0, multiplier: 1 });
    }
  }

  private spec(id: string) { return this.specs.find((h) => h.id === id); }
  private list(id: string): Patient[] { return this.patients.get(id) ?? []; }
  private stats(id: string): Counters | undefined { return this.counters.get(id); }

  capacity(id: string): number { return this.spec(id)?.beds ?? 0; }
  occupied(id: string): number { return this.list(id).length; }
  free(id: string): number { return Math.max(0, this.capacity(id) - this.occupied(id)); }
  hasAdmittedIncident(incidentId: string): boolean { return this.admittedIncidents.has(incidentId); }

  /** A normal day at t0: each hospital part-full, every patient part-way through a stay. */
  seedNormalDay(t0: number): void {
    for (const h of this.specs) {
      const n = Math.min(h.beds, Math.round(h.beds * NORMAL_OCCUPANCY * (0.8 + this.rand() * 0.4)));
      for (let i = 0; i < n; i++) {
        const acuity = sampleAcuity(1, this.rand);
        const los = sampleStayS(acuity, this.rand);
        // already in for part of the stay: a uniformly random part, so discharges are spread out from the start
        this.addPatient(h.id, { source: 'walk_in', acuity, admitted_at_s: t0 - this.rand() * los, los_s: los }, false);
      }
    }
  }

  /** Put a patient in a bed if there is one. Returns the patient, or null when the hospital has no free bed. */
  admit(hospitalId: string, p: { source: PatientSource; acuity: Acuity; incident_id?: string; t: number; los_s?: number }): Patient | null {
    if (this.free(hospitalId) <= 0) return null;
    const los = p.los_s ?? sampleStayS(p.acuity, this.rand);
    return this.addPatient(hospitalId, { source: p.source, acuity: p.acuity, incident_id: p.incident_id, admitted_at_s: p.t, los_s: los }, true);
  }

  private addPatient(hospitalId: string, p: Omit<Patient, 'id' | 'discharge_at_s'>, count: boolean): Patient {
    this.nextPatient += 1;
    const patient: Patient = { ...p, id: `p${this.nextPatient}`, discharge_at_s: p.admitted_at_s + p.los_s };
    this.list(hospitalId).push(patient);
    if (p.incident_id) this.admittedIncidents.add(p.incident_id);
    const st = this.stats(hospitalId);
    if (count && st && p.source === 'walk_in') st.walkIns += 1;
    return patient;
  }

  /** Free every bed whose patient's stay has ended. Returns what was discharged, per hospital. */
  dischargeDue(t: number): Map<string, Patient[]> {
    const out = new Map<string, Patient[]>();
    for (const [id, list] of this.patients) {
      const gone = list.filter((p) => p.discharge_at_s <= t);
      if (!gone.length) continue;
      this.patients.set(id, list.filter((p) => p.discharge_at_s > t));
      const st = this.stats(id);
      if (st) st.discharged += gone.length;
      out.set(id, gone);
    }
    return out;
  }

  /** Walk-ins that arrive in the next `dt` sim-seconds, as (hospital the patient goes to first, acuity). */
  sampleArrivals(dt: number, hazards: Hazard[]): { hospital_id: string; acuity: Acuity }[] {
    const out: { hospital_id: string; acuity: Acuity }[] = [];
    for (const h of this.specs) {
      const m = demandMultiplier(h, hazards);
      const st = this.stats(h.id);
      if (st) st.multiplier = m;
      const n = poisson(NORMAL_ARRIVALS_PER_BED_S * h.beds * m * dt, this.rand);
      if (st) st.arrivals += n;
      for (let i = 0; i < n; i++) out.push({ hospital_id: h.id, acuity: sampleAcuity(m, this.rand) });
    }
    return out;
  }

  noteDiverted(from: string, to: string | null): void {
    const a = this.stats(from);
    if (a) a.divertedOut += 1;
    const b = to ? this.stats(to) : undefined;
    if (b) b.divertedIn += 1;
  }
  noteOverflow(hospitalId: string): void {
    const st = this.stats(hospitalId);
    if (st) st.overflow += 1;
  }

  /** Operator: take `n` free beds (patients held on the ward). */
  occupyBeds(hospitalId: string, n: number, t: number): number {
    let done = 0;
    for (let i = 0; i < n; i++) {
      if (this.admit(hospitalId, { source: 'surge', acuity: 'moderate', t })) done += 1; else break;
    }
    return done;
  }

  /** Operator: free `n` beds now by discharging the patients who are closest to leaving anyway (walk-ins first). */
  releaseBeds(hospitalId: string, n: number): number {
    const list = this.list(hospitalId);
    const rank = (p: Patient) => (p.source === 'incident' ? 1 : 0);
    const order = [...list].sort((a, b) => rank(a) - rank(b) || a.discharge_at_s - b.discharge_at_s).slice(0, n);
    const drop = new Set(order.map((p) => p.id));
    this.patients.set(hospitalId, list.filter((p) => !drop.has(p.id)));
    const st = this.stats(hospitalId);
    if (st) st.discharged += order.length;
    return order.length;
  }

  totalOccupied(): number { let n = 0; for (const l of this.patients.values()) n += l.length; return n; }
  totalCapacity(): number { return this.specs.reduce((s, h) => s + h.beds, 0); }

  census(t: number, status: Record<string, 'open' | 'full' | 'offline'>): Record<string, HospitalLoad> {
    const out: Record<string, HospitalLoad> = {};
    for (const h of this.specs) {
      const list = this.list(h.id);
      const st = this.stats(h.id)!;
      const incident: HospitalPatient[] = list.filter((p) => p.source === 'incident').map((p) => ({
        id: p.id, incident_id: p.incident_id ?? '', admitted_at_s: Math.round(p.admitted_at_s),
        los_min: Math.round(p.los_s / SIM_MIN), discharge_at_s: Math.round(p.discharge_at_s),
      }));
      const next = list.length ? Math.max(0, Math.round(Math.min(...list.map((p) => p.discharge_at_s)) - t)) : null;
      out[h.id] = {
        capacity: h.beds, occupied: list.length, free: Math.max(0, h.beds - list.length),
        occupancy: h.beds ? Math.min(1, list.length / h.beds) : 0,
        status: status[h.id] ?? 'open',
        demand_multiplier: Math.round(st.multiplier * 100) / 100,
        walk_in_patients: list.filter((p) => p.source === 'walk_in').length,
        incident_patients: incident.length,
        surge_patients: list.filter((p) => p.source === 'surge').length,
        arrivals_total: st.arrivals, walk_ins_total: st.walkIns, discharged_total: st.discharged,
        diverted_out: st.divertedOut, diverted_in: st.divertedIn, overflow: st.overflow,
        next_discharge_s: next, patients: incident,
      };
    }
    return out;
  }

  admittedIncidentIds(): string[] { return [...this.admittedIncidents]; }

  /** Patients only (counters are history, not state): what Undo restores. */
  save(): LedgerState {
    const patients: Record<string, Patient[]> = {};
    for (const [id, l] of this.patients) patients[id] = l.map((p) => ({ ...p }));
    return { patients, admittedIncidents: [...this.admittedIncidents] };
  }
  restore(s: LedgerState): void {
    for (const id of this.patients.keys()) this.patients.set(id, (s.patients[id] ?? []).map((p) => ({ ...p })));
    this.admittedIncidents = new Set(s.admittedIncidents);
  }
}
