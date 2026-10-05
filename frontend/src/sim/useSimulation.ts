import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { clearDemoReports } from '../api/reportsDemo';
import { CITY } from '../lib/layers';
import { useCityVersion } from '../lib/useCity';
import { post } from '../shared/bus';
import { mergeIncidents } from '../shared/incidents';
import { useLive } from '../shared/live';
import type { Incident, PlanItem, RouteInfo, WorldState } from '../shared/types';
import { SimWorld, computeScore, type WorldSnapshot } from './world';

const TICK_MS = 500;
/** Longest real gap (s) credited to one tick. Guards against a suspended laptop / throttled background tab jumping the world. */
const MAX_REAL_DT_S = 10;
const EMPTY_ITEMS: PlanItem[] = [];
const EMPTY_ROUTES: RouteInfo[] = [];

export function useSimulation() {
  const [runId, setRunId] = useState<string | null>(null);
  const [starting, setStarting] = useState(false);
  const [startError, setStartError] = useState<string | null>(null);
  const [pubError, setPubError] = useState<string | null>(null);
  const [running, setRunning] = useState(false);
  const [speed, setSpeed] = useState(5);
  const [autoReplan, setAutoReplan] = useState(false);
  const [escalateAfterS, setEscalateAfterS] = useState(120);

  // Manual incidents as the backend returned them. Shown until the live stream carries the backend's own copy.
  const [created, setCreated] = useState<Incident[]>([]);

  const worldRef = useRef(new SimWorld());
  const [snap, setSnap] = useState<WorldSnapshot>(() => worldRef.current.snapshot());

  // A different city was loaded while idle: the world was built from the old city's hospitals and flood zones, so rebuild it.
  // (The map locks the city once a run starts, so this never discards a running simulation.)
  const cityVersion = useCityVersion();
  useEffect(() => {
    if (runIdRef.current != null) return;
    worldRef.current = new SimWorld();
    setSnap(worldRef.current.snapshot());
  }, [cityVersion]);

  // the simulator pins its own run: it never follows a run started from somewhere else
  const live = useLive({
    // the Route Agent's answer to a re-route request (from Authority or from auto re-plan)
    onReplan: (p) => {
      if (p.run_id && runId && p.run_id !== runId) return;
      worldRef.current.applyReplan(p);
      setSnap(worldRef.current.snapshot());
    },
  }, runId);
  const { payload, reports } = live;

  const backendIncidents = useMemo(
    () => mergeIncidents({ backend: payload?.incidents ?? [], verifications: undefined, reports, sim: [], override: {} }),
    [payload, reports],
  );
  const incidents: Incident[] = useMemo(
    () => mergeIncidents({ backend: payload?.incidents ?? [], reports, sim: [...created, ...snap.sim_incidents], override: snap.urgency_override }),
    [payload, reports, created, snap.sim_incidents, snap.urgency_override],
  );

  const inputsRef = useRef({ incidents: backendIncidents, items: EMPTY_ITEMS, routes: EMPTY_ROUTES, hospitals: payload?.hospital_assignments ?? [], autoReplan, escalateAfterS });
  inputsRef.current = {
    incidents: backendIncidents,
    items: payload?.plan?.items ?? EMPTY_ITEMS,
    routes: payload?.routes ?? EMPTY_ROUTES,
    hospitals: payload?.hospital_assignments ?? [],
    autoReplan,
    escalateAfterS,
  };
  const runIdRef = useRef<string | null>(runId);
  runIdRef.current = runId;
  const speedRef = useRef(speed);
  speedRef.current = speed;

  /* publish world + discrete events to the backend (throttled, trailing) */
  const lastPub = useRef(0);
  const pubTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const publish = useCallback(() => {
    const send = () => {
      pubTimer.current = null;
      lastPub.current = Date.now();
      const s = worldRef.current.snapshot();
      const w: WorldState = {
        run_id: runIdRef.current,
        vehicles: s.vehicles, disruptions: s.disruptions, affected_regions: s.affected_regions, sim_incidents: s.sim_incidents,
        execution: s.execution, events: s.events.slice(-50), beds: s.beds, hospital_status: s.hospital_status, hospital_load: s.hospital_load, admitted_incident_ids: s.admitted_incident_ids,
        urgency_override: s.urgency_override,
        incident_status: s.incident_status,
        sim_time_s: s.sim_time_s, ts: s.ts,
      };
      post('/sim/world', w).then(() => setPubError(null)).catch((e) => setPubError(e instanceof Error ? e.message : 'Publish failed'));
      for (const ev of worldRef.current.drainEvents()) post('/sim/event', ev).catch(() => undefined);
    };
    const wait = 300 - (Date.now() - lastPub.current);
    if (wait <= 0) send();
    else if (!pubTimer.current) pubTimer.current = setTimeout(send, wait);
  }, []);
  useEffect(() => () => { if (pubTimer.current) clearTimeout(pubTimer.current); }, []);

  const flush = useCallback(() => {
    // vehicles that stopped on a blocked road ask the Route Agent for a new route; its answer comes back on the bus
    const rid = runIdRef.current;
    for (const itemId of worldRef.current.drainRerouteRequests()) {
      if (!rid) { worldRef.current.rerouteFailed(itemId); continue; }
      post(`/plan/${rid}/replan`, { item_id: itemId, action: 'reroute' }).catch(() => worldRef.current.rerouteFailed(itemId));
    }
    setSnap(worldRef.current.snapshot());
    publish();
  }, [publish]);

  useEffect(() => {
    if (!running) return;
    // Sim progress = real time elapsed since the last tick x the speed in force right now. Resuming (this effect
    // re-running) restarts the clock, so paused time is never counted, and a speed change only affects future ticks.
    let last = performance.now();
    const id = setInterval(() => {
      const nowMs = performance.now();
      const realDt = Math.min(MAX_REAL_DT_S, Math.max(0, (nowMs - last) / 1000));
      last = nowMs;
      const i = inputsRef.current;
      worldRef.current.tick(realDt * speedRef.current, {
        incidents: i.incidents, planItems: i.items, routes: i.routes, hospitalAssignments: i.hospitals,
        autoReplan: i.autoReplan, escalateAfterS: i.escalateAfterS,
      });
      flush();
    }, TICK_MS);
    return () => clearInterval(id);
  }, [running, flush]);

  const start = useCallback(async () => {
    setStarting(true);
    setStartError(null);
    try {
      const res = await post<{ run_id: string }>('/scenario/start', { replay_speed: Math.max(1, speed), simulate_failures: [], city: CITY.slug });
      // a new sim becomes the active run: drop browser-only demo reports so they cannot leak into it
      clearDemoReports();
      runIdRef.current = res.run_id;
      setRunId(res.run_id);
      setRunning(true);
    } catch (e) {
      setStartError(e instanceof Error ? e.message : 'Failed to start the scenario');
    } finally {
      setStarting(false);
    }
  }, [speed]);

  const reset = useCallback(() => {
    setCreated([]);
    setRunning(false);
    setRunId(null);
    runIdRef.current = null;
    clearDemoReports();
    worldRef.current = new SimWorld();
    flush();
  }, [flush]);

  /** Create a real incident on the backend. It is planned by the agents, approved by Authority and dispatched like any other. */
  const addIncident = useCallback(async (p: { text: string; location: string; lat: number; lng: number; need_type: Incident['need_type']; urgency: Incident['urgency']; people: number }) => {
    const rid = runIdRef.current;
    if (!rid) throw new Error('Start the simulation first: incidents are planned by the backend inside a run.');
    const inc = await post<Incident>('/incidents', { ...p, run_id: rid });
    setCreated((prev) => (prev.some((x) => x.id === inc.id) ? prev : [...prev, inc]));
    worldRef.current.noteIncidentAdded(inc);
    flush();
    return inc;
  }, [flush]);

  /** Run any world mutation (tool action), then refresh + publish. */
  const act = useCallback((fn: (w: SimWorld) => void) => {
    fn(worldRef.current);
    flush();
  }, [flush]);

  const score = useMemo(() => computeScore(snap, incidents), [snap, incidents]);
  const approvedKeys = useMemo(
    () => new Set((payload?.plan?.items ?? []).filter((i) => i.status === 'approved' || i.status === 'edited').map((i) => i.id)),
    [payload],
  );

  return {
    runId, starting, startError, pubError, running, setRunning, speed, setSpeed, autoReplan, setAutoReplan,
    escalateAfterS, setEscalateAfterS, incidents, snap, score, approvedKeys, payload, connected: live.connected,
    start, reset, act, addIncident,
  };
}
