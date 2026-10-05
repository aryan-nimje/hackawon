// Headless check of the hospital model. Run (from frontend/):
//   npx esbuild scripts/check-hospitals.ts --bundle --platform=node --format=esm --outfile=/tmp/check.mjs --define:import.meta.env='{}' && node /tmp/check.mjs
import assert from 'node:assert/strict';
import { HospitalLedger, demandMultiplier, SIM_MIN, type Hazard } from '../src/sim/hospitals';
import { SimWorld } from '../src/sim/world';
import { HOSPITALS } from '../src/lib/layers';

function mulberry(a: number) { return () => { a |= 0; a = (a + 0x6d2b79f5) | 0; let t = Math.imul(a ^ (a >>> 15), 1 | a); t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t; return ((t ^ (t >>> 14)) >>> 0) / 4294967296; }; }

// ── 1. demand scales with severity and nearness
const sassoon = HOSPITALS[0], noble = HOSPITALS[6];
const hz = (sev: any, scale = 1): Hazard[] => [{ lat: sassoon.lat + 0.002, lng: sassoon.lng, radius_km: 0.4, severity: sev, scale }];
const m = (h: any, hs: Hazard[]) => demandMultiplier(h, hs);
assert.equal(m(sassoon, []), 1);
assert(m(sassoon, hz('low')) < m(sassoon, hz('medium')) && m(sassoon, hz('medium')) < m(sassoon, hz('high')) && m(sassoon, hz('high')) < m(sassoon, hz('critical')));
assert(m(sassoon, hz('high')) > m(noble, hz('high')), 'near hazard > far');
assert(m(sassoon, hz('high', 2)) > m(sassoon, hz('high', 1)), 'bigger hazard > smaller');
console.log('demand ok: sassoon/noble @high', m(sassoon, hz('high')).toFixed(2), m(noble, hz('high')).toFixed(2));

// ── 2. stay: bed stays occupied until the stay ends, then frees
const L = new HospitalLedger([{ id: 'x', lat: 0, lng: 0, beds: 2 }], mulberry(1));
const p = L.admit('x', { source: 'walk_in', acuity: 'minor', t: 100, los_s: 600 })!;
assert.equal(p.discharge_at_s, 700);
L.admit('x', { source: 'incident', acuity: 'major', incident_id: 'i1', t: 100, los_s: 3600 });
assert.equal(L.free('x'), 0);
assert.equal(L.admit('x', { source: 'walk_in', acuity: 'minor', t: 100 }), null, 'no bed when full');
L.dischargeDue(699); assert.equal(L.free('x'), 0, 'still occupied before stay ends');
L.dischargeDue(700); assert.equal(L.free('x'), 1, 'freed when stay ends');
assert(L.admit('x', { source: 'walk_in', acuity: 'minor', t: 700 }), 'freed bed reusable');
assert(L.hasAdmittedIncident('i1'));
// undo round trip
const sv = L.save(); L.releaseBeds('x', 2); assert.equal(L.free('x'), 2); L.restore(sv); assert.equal(L.free('x'), 0);
console.log('ledger ok');

// ── 3. world timeline
function run(label: string, setup: (w: SimWorld) => void, hours = 3) {
  const w = new SimWorld(mulberry(42));
  setup(w);
  const input = { incidents: [], planItems: [], routes: [], hospitalAssignments: [], autoReplan: false, escalateAfterS: 1e9 } as any;
  const dt = 2.5; let firstFull = -1, firstDivert = -1, firstOverflow = -1;
  const rows: string[] = [];
  for (let t = 0; t < hours * 3600; t += dt) {
    w.tick(dt, input);
    const s = w.snapshot();
    // consistency every tick
    for (const h of HOSPITALS) {
      const hl = s.hospital_load![h.id];
      assert.equal(s.beds[h.id], h.beds - hl.occupied, 'beds == capacity - occupied');
      assert(hl.occupied >= 0 && hl.occupied <= h.beds, 'never over capacity');
      assert.equal(hl.free, s.beds[h.id]);
    }
    if (firstFull < 0 && HOSPITALS.some((h) => s.beds[h.id] === 0)) firstFull = Math.round(w.t / 60);
    if (firstDivert < 0 && s.diverted > 0) firstDivert = Math.round(w.t / 60);
    if (firstOverflow < 0 && s.overflow > 0) firstOverflow = Math.round(w.t / 60);
    if (Math.round(w.t) % 1800 === 0 && (Math.round(w.t) !== (rows as any).last)) {
      rows.push(`t=${String(Math.round(w.t / 60)).padStart(3)}m ` + HOSPITALS.map((h) => `${s.hospital_load![h.id].occupied}/${h.beds}`).join(' ') + `  div=${s.diverted} ovf=${s.overflow}`);
    }
  }
  const s = w.snapshot();
  console.log(`\n== ${label}\nfirst hospital full @${firstFull}m, first divert @${firstDivert}m, first overflow @${firstOverflow}m`);
  console.log([...new Set(rows)].join('\n'));
  console.log('multipliers', HOSPITALS.map((h) => s.hospital_load![h.id].demand_multiplier).join(' '));
  return s;
}
const base = run('base scenario (3 base flood zones)', () => {});
const severe = run('+ critical earthquake at Sassoon', (w) => w.addRegion('collapse', sassoon.lat, sassoon.lng, 800, 'critical', true));
const sum = (s: any) => HOSPITALS.reduce((a, h) => a + s.hospital_load![h.id].arrivals_total, 0);
assert(sum(severe) > sum(base), 'more walk-in demand with severe disaster');
console.log('\nwalk-ins total base vs severe', sum(base), sum(severe));

// ── 4. recovery
{
  // (a) with no new arrivals every bed is freed by the end of the longest stay (120 sim-min), monotonically
  const L2 = new HospitalLedger([{ id: 'y', lat: 0, lng: 0, beds: 40 }], mulberry(5));
  L2.seedNormalDay(0);
  const start = L2.occupied('y'); assert(start > 10);
  let prev = start;
  for (let t = 60; t <= 130 * 60; t += 60) { L2.dischargeDue(t); assert(L2.occupied('y') <= prev); prev = L2.occupied('y'); }
  assert.equal(L2.occupied('y'), 0, 'every stay ends, every bed comes back');
  // (b) the hospital at the hazard is fuller with the hazard than after it is removed
  const w = new SimWorld(mulberry(7));
  w.addRegion('collapse', sassoon.lat, sassoon.lng, 800, 'critical', false);
  const input = { incidents: [], planItems: [], routes: [], hospitalAssignments: [], autoReplan: false, escalateAfterS: 1e9 } as any;
  const meanOcc = (secs: number) => { let a = 0, n = 0; for (let t = 0; t < secs; t += 2.5) { w.tick(2.5, input); a += w.snapshot().hospital_load![sassoon.id].occupancy; n += 1; } return a / n; };
  meanOcc(2 * 3600);
  const withHazard = meanOcc(3600);
  w.removeRegion(w.snapshot().affected_regions.find((r) => !r.id.startsWith('flood'))!.id);
  meanOcc(2 * 3600);
  const after = meanOcc(3600);
  console.log('\nSassoon mean occupancy: with quake', withHazard.toFixed(2), '-> 2h after it is removed', after.toFixed(2));
  assert(after < withHazard - 0.05, 'occupancy falls once the surge ends');
}

// ── 5. incident patient through the world: takes a bed for the agent's expected stay, then frees it
{
  const w = new SimWorld(mulberry(3)) as any;
  const h = HOSPITALS[6];
  const before = w.ledger.free(h.id);
  const asg = { incident_id: 'inc-1', hospital_id: h.id, hospital_name: h.name, distance_km: 1, explanation: '', specialty_match: true, expected_stay_min: 30 };
  w.t = 1000;
  w.admitPatient('inc-1', [asg], [h.lat, h.lng]);
  assert.equal(w.ledger.free(h.id), before - 1);
  const rec = w.snapshot().hospital_load[h.id].patients.find((x: any) => x.incident_id === 'inc-1');
  assert.equal(rec.los_min, 30); assert.equal(rec.discharge_at_s, 1000 + 1800);
  assert(w.snapshot().admitted_incident_ids.includes('inc-1'));
  w.ledger.dischargeDue(1000 + 1799); assert(w.snapshot().hospital_load[h.id].patients.some((x: any) => x.incident_id === 'inc-1'), 'still in bed 1s before end');
  w.ledger.dischargeDue(1000 + 1800); assert(!w.snapshot().hospital_load[h.id].patients.some((x: any) => x.incident_id === 'inc-1'), 'discharged at end of stay');
  assert(w.snapshot().admitted_incident_ids.includes('inc-1'), 'stays in the admitted history');
  // full hospital -> diverted
  w.ledger.occupyBeds(h.id, w.ledger.free(h.id), w.t);
  w.admitPatient('inc-2', [{ ...asg, incident_id: 'inc-2' }], [h.lat, h.lng]);
  assert.equal(w.snapshot().diverted, 1);
  // everything full -> overflow
  for (const x of HOSPITALS) w.ledger.occupyBeds(x.id, w.ledger.free(x.id), w.t);
  w.admitPatient('inc-3', [{ ...asg, incident_id: 'inc-3' }], [h.lat, h.lng]);
  assert.equal(w.snapshot().overflow >= 1, true);
  console.log('\nincident patient flow ok');
}
// ── 6. operator controls go through the ledger, and Undo restores it
{
  const w = new SimWorld(mulberry(9)) as any;
  const h = HOSPITALS[1]; const id = h.id;
  const free0 = w.ledger.free(id);
  w.setBeds(id, 0);
  assert.equal(w.snapshot().beds[id], 0); assert.equal(w.snapshot().hospital_load[id].occupied, h.beds);
  assert(w.snapshot().hospital_load[id].surge_patients > 0);
  w.undo(); assert.equal(w.ledger.free(id), free0, 'undo restores the beds');
  w.markFull(id, true);
  assert.equal(w.snapshot().hospital_status[id], 'full'); assert.equal(w.ledger.free(id), 0);
  assert.equal(w.hasRoom(id), false, 'a full hospital takes nobody');
  w.markFull(id, false);
  assert(w.ledger.free(id) >= Math.ceil(h.beds * 0.2), 'reopens with at least 20% free');
  w.setOffline(id, true); assert.equal(w.hasRoom(id), false);
  // walk-ins from an offline hospital's catchment are diverted, not lost
  const before = w.snapshot().diverted;
  for (let i = 0; i < 40; i++) w.placeWalkIn(id, 'minor');
  assert(w.snapshot().diverted > before, 'offline hospital diverts walk-ins');
  w.setOffline(id, false);
  console.log('operator controls ok');
}
console.log('ALL OK');
