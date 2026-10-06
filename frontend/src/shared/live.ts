import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { get, useBus } from './bus';
import { clean } from './clean';
import { EMPTY_WORLD } from './types';
import type { CitizenReport, PlanPayload, ReplanResult, SimBusEvent, WorldState } from './types';

export interface LiveHandlers {
  onSimEvent?: (e: SimBusEvent) => void;
  onReport?: (r: CitizenReport) => void;
  onReplan?: (p: ReplanResult) => void;
  onPlanApproved?: (p: PlanPayload) => void;
}

/** Keep only the few most recent runs so a long session does not grow without bound. */
const MAX_RUNS = 5;

/**
 * Everything a client can receive from the backend stream. Used by all three apps.
 *
 * Active run: the run the backend declares with `run.active` (set when a scenario starts), else the run in
 * `world.run_id`, else the run of the newest plan payload. Plan payloads are stored per run and only the active run's is exposed, so old
 * runs are hidden (not deleted) and a late message from an old run cannot overwrite the current one.
 * Reports tagged with a different run are hidden; untagged (real) reports are always shown.
 * Pass `pinRunId` (the simulator does) to follow exactly one run regardless of what other clients publish.
 */
export function useLive(h: LiveHandlers = {}, pinRunId?: string | null) {
  const [payloads, setPayloads] = useState<Record<string, PlanPayload>>({});
  const [latestRun, setLatestRun] = useState<string | null>(null);
  const [world, setWorld] = useState<WorldState>(EMPTY_WORLD);
  const [allReports, setAllReports] = useState<CitizenReport[]>([]);
  const [simEvents, setSimEvents] = useState<SimBusEvent[]>([]);
  // The run the backend declared active (`run.active`, sent when a scenario starts and on every connect).
  const [serverRun, setServerRun] = useState<string | null>(null);
  // City (slug) the active run plans for, as the backend declared it with `run.active`.
  const [runCity, setRunCity] = useState<string | null>(null);
  const hr = useRef(h);
  hr.current = h;

  const activeRunId = pinRunId ?? serverRun ?? world.run_id ?? latestRun;

  const activeRef = useRef<string | null>(activeRunId);
  activeRef.current = activeRunId;

  const store = useCallback((d: PlanPayload) => {
    if (!d || typeof d.run_id !== 'string') return;
    d = { ...d, incidents: (d.incidents ?? []).map((i) => ({ ...i, text: clean(i.text), location: clean(i.location) })) };
    setPayloads((prev) => {
      const next = { ...prev, [d.run_id]: d };
      const keys = Object.keys(next);
      if (keys.length > MAX_RUNS) for (const k of keys.slice(0, keys.length - MAX_RUNS)) if (k !== d.run_id) delete next[k];
      return next;
    });
    setLatestRun(d.run_id);
  }, []);

  const addReport = useCallback((r: CitizenReport, notify: boolean) => {
    setAllReports((prev) => {
      if (prev.some((x) => x.token === r.token)) return prev;
      // only announce reports that belong to the run we are showing
      if (notify && (!r.run_id || r.run_id === activeRef.current || activeRef.current == null)) hr.current.onReport?.(r);
      return [r, ...prev].slice(0, 200);
    });
  }, []);

  const { connected } = useBus({
    'plan.updated': (d) => store(d as PlanPayload),
    'plan.approved': (d) => {
      store(d as PlanPayload);
      const p = d as PlanPayload;
      if (!activeRef.current || p.run_id === activeRef.current) hr.current.onPlanApproved?.(p);
    },
    'plan.replan': (d) => hr.current.onReplan?.(d as ReplanResult),
    'run.active': (d) => {
      const id = (d as { run_id?: string | null })?.run_id;
      if (!id) return;
      setRunCity((d as { city?: string | null })?.city ?? null);
      // A new sim started: switch to it, drop the old world, and ignore anything still tagged with another run.
      setServerRun((prev) => {
        if (prev !== id) {
          setWorld(EMPTY_WORLD);
          setSimEvents([]);
          setAllReports([]);
        }
        return id;
      });
    },
    // "Reset simulation" removed simulation runs: forget them and follow the real run the backend restored (or none).
    'sim.reset': (d) => {
      const p = (d ?? {}) as { removed_run_ids?: string[]; run_id?: string | null; city?: string | null };
      const removed = new Set(p.removed_run_ids ?? []);
      setPayloads((prev) => Object.fromEntries(Object.entries(prev).filter(([k]) => !removed.has(k))));
      setLatestRun((cur) => (cur && removed.has(cur) ? null : cur));
      setAllReports((prev) => prev.filter((r) => !r.run_id || !removed.has(r.run_id)));
      setSimEvents([]);
      setWorld(EMPTY_WORLD);
      setRunCity(p.city ?? null);
      setServerRun(p.run_id ?? null);
    },
    world: (d) => {
      const w = d as WorldState;
      const active = activeRef.current;
      if (w.run_id && active && w.run_id !== active) return; // another run's world must not replace this one
      setWorld(w);
    },
    'execution.update': (d) => setWorld((w) => ({ ...w, execution: d as WorldState['execution'] })),
    'report.new': (d) => addReport(d as CitizenReport, true),
    sim_event: (d) => {
      const e = d as SimBusEvent & { run_id?: string | null };
      if (e.run_id && activeRef.current && e.run_id !== activeRef.current) return;
      setSimEvents((p) => [e, ...p].slice(0, 100));
      hr.current.onSimEvent?.(e);
    },
  }, () => {
    // (Re)connected: forget everything held locally. The backend replays what still exists, so data that was
    // cleared on the server (restart, DB wipe, reset) no longer lingers on screen.
    setPayloads({}); setLatestRun(null); setWorld(EMPTY_WORLD); setAllReports([]); setSimEvents([]);
    setServerRun(null); setRunCity(null);
  });

  // Reports: the server list is the truth. Re-read it on connect and every 15 s so rows removed from the
  // database disappear here too (the stream only ever announces new reports, never deletions).
  useEffect(() => {
    if (!connected) return;
    let alive = true;
    const sync = () => get<CitizenReport[]>('/reports').then((list) => {
      if (!alive) return;
      setAllReports((prev) => {
        const known = new Set(prev.map((r) => r.token));
        list.forEach((r) => { if (!known.has(r.token) && (!r.run_id || r.run_id === activeRef.current || activeRef.current == null)) hr.current.onReport?.(r); });
        return [...list].reverse().slice(0, 200);
      });
    }).catch(() => undefined);
    void sync();
    const id = setInterval(sync, 15000);
    return () => { alive = false; clearInterval(id); };
  }, [connected, serverRun]);

  const payload = activeRunId ? payloads[activeRunId] ?? null : null;
  const reports = useMemo(
    () => allReports.filter((r) => !r.run_id || r.run_id === activeRunId),
    [allReports, activeRunId],
  );

  return { connected, payload, world, reports, simEvents, activeRunId, runCity };
}
