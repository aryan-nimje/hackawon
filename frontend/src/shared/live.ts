import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { get, useBus } from './bus';
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
  });

  // initial citizen reports once the stream is up
  useEffect(() => {
    if (!connected) return;
    get<CitizenReport[]>('/reports').then((list) => list.forEach((r) => addReport(r, false))).catch(() => undefined);
  }, [connected, serverRun, addReport]);

  const payload = activeRunId ? payloads[activeRunId] ?? null : null;
  const reports = useMemo(
    () => allReports.filter((r) => !r.run_id || r.run_id === activeRunId),
    [allReports, activeRunId],
  );

  return { connected, payload, world, reports, simEvents, activeRunId, runCity };
}
