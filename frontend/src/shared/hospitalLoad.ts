import type { HospitalLoad, HospitalStatus, WorldState } from './types';

/** Colour of an occupancy bar / map marker: calm, busy, critical; grey when the hospital is offline. */
export function occupancyColor(occupancy: number, status: HospitalStatus = 'open'): string {
  if (status === 'offline') return '#57534e';
  if (status === 'full' || occupancy >= 0.9) return '#991b1b';
  if (occupancy >= 0.7) return '#d9a406';
  return '#15803d';
}

export interface HospitalView {
  capacity: number;
  occupied: number;
  free: number;
  occupancy: number;
  status: HospitalStatus;
  /** open but out of beds: new patients are being sent elsewhere */
  atCapacity: boolean;
  load?: HospitalLoad;
}

/** One hospital as the UI shows it. Uses the census when the world has one; otherwise derives it from free beds (older senders). */
export function hospitalView(h: { id: string; beds: number }, world: Pick<WorldState, 'beds' | 'hospital_status' | 'hospital_load'>): HospitalView {
  const load = world.hospital_load?.[h.id];
  const status = world.hospital_status?.[h.id] ?? load?.status ?? 'open';
  const free = load ? load.free : Math.max(0, Math.min(h.beds, world.beds[h.id] ?? h.beds));
  const occupied = load ? load.occupied : h.beds - free;
  const occupancy = h.beds ? occupied / h.beds : 0;
  return { capacity: h.beds, occupied, free, occupancy, status, atCapacity: status === 'open' && free <= 0, load };
}
