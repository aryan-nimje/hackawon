import type { CitizenReport, Incident, IncidentStatus, IncidentStatusEntry, Urgency, VerificationResult } from './types';

export function reportToIncident(r: CitizenReport): Incident {
  const s = r.submission;
  const need = s.need_type === 'supplies' ? 'supplies' : (s.need_type ?? 'rescue');
  const urgency: Urgency = r.urgency ?? (s.vulnerable.length || s.need_type === 'medical' ? 'high' : 'medium');
  return {
    // Database reports use their backend incident id (`cr-<uuid>`), so the run's own copy of the same report wins the de-dupe.
    id: r.token.startsWith('cr-') ? r.token : `cit-${r.token.slice(5, 11)}`,
    text: s.text, location: s.location_text || `${s.lat.toFixed(4)}, ${s.lng.toFixed(4)}`,
    lat: s.lat, lng: s.lng, need_type: need, urgency, source: 'citizen', timestamp: r.created_at,
    raw_metadata: { vulnerable: s.vulnerable, people: s.people_count }, verification: null,
  };
}

export function mergeIncidents(opts: {
  backend: Incident[]; verifications?: VerificationResult[]; reports: CitizenReport[]; sim: Incident[]; override: Record<string, Urgency>;
}): Incident[] {
  const ver = new Map((opts.verifications ?? []).map((v) => [v.incident_id, v]));
  const all = [
    ...opts.backend.map((i) => ({ ...i, verification: i.verification ?? ver.get(i.id) ?? null })),
    ...opts.reports.map(reportToIncident),
    ...opts.sim,
  ];
  const seen = new Set<string>();
  const out: Incident[] = [];
  for (const i of all) {
    if (seen.has(i.id)) continue;
    seen.add(i.id);
    out.push(opts.override[i.id] ? { ...i, urgency: opts.override[i.id] } : i);
  }
  return out;
}

export const isFeedIncident = (i: Incident) => i.source === 'weather' || i.source === 'news' || i.source === 'system';

/** Spam / duplicate / retracted / unlocated reports. They never get a team, so they have no lifecycle. */
export function isJunkIncident(inc: Incident): boolean {
  const m = inc.raw_metadata ?? {};
  return !!(m.suspicious || m.duplicate_of || m.retraction) || (Math.abs(inc.lat) < 1 && Math.abs(inc.lng) < 1);
}

/** Reports that go through the assign -> resolve / expire lifecycle. */
export const isTrackedIncident = (i: Incident) => !isFeedIncident(i) && !isJunkIncident(i);

export type IncidentStatusMap = Record<string, IncidentStatusEntry> | undefined;

export function incidentStatusOf(map: IncidentStatusMap, id: string): IncidentStatus {
  return map?.[id]?.status ?? 'open';
}

export const isTerminalStatus = (s: IncidentStatus) => s === 'resolved' || s === 'expired';

export interface IncidentCounts { open: number; assigned: number; resolved: number; expired: number }

/** `open` counts everything still live (awaiting a team + assigned); `assigned` is the subset with a team. */
export function countIncidentStatuses(incidents: Incident[], map: IncidentStatusMap): IncidentCounts {
  const c: IncidentCounts = { open: 0, assigned: 0, resolved: 0, expired: 0 };
  for (const i of incidents) {
    if (!isTrackedIncident(i)) continue;
    const s = incidentStatusOf(map, i.id);
    if (s === 'resolved') c.resolved += 1;
    else if (s === 'expired') c.expired += 1;
    else { c.open += 1; if (s === 'assigned') c.assigned += 1; }
  }
  return c;
}

/** How long (sim-seconds) a resolved/expired incident takes to fade off the map and feed. */
export const INCIDENT_FADE_S = 90;

/** 1 = fully visible, 0 = gone. Live incidents are always 1; finished ones fade linearly over INCIDENT_FADE_S sim-seconds. */
export function incidentOpacity(entry: IncidentStatusEntry | undefined, simTimeS: number): number {
  if (!entry || !isTerminalStatus(entry.status) || entry.ended_at_s == null) return 1;
  const f = (simTimeS - entry.ended_at_s) / INCIDENT_FADE_S;
  return f >= 1 ? 0 : Math.max(0.15, 1 - 0.85 * Math.max(0, f));
}
